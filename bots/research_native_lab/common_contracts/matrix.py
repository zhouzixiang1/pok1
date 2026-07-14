"""Complete preregistered comparison matrix and global result ledger.

This module is the formal fairness boundary for the three research routes.  A
``CompleteFormalMatrix`` is the *only* pre-beacon freeze root.  It contains all
four main artifacts per route (two checkpoint policies crossed with two
comparison modes), every mandatory ablation or an explicit not-applicable
record, fixed visible opponents, an opaque salted held-out commitment, exact
resource cells, and the complete hypothesis family.

After a trustworthy frozen future beacon is verified, ``materialize_formal_matrix``
checks the held-out reveal and creates a ``FormalMatrixProjection``.  That
formal path currently fails closed because the repository's Bitcoin/OTS
authority is not independently rooted; the diagnostic projection exercises
the deterministic mapping without strength authority.  A projection is a view
of the frozen root, not a second freeze root.  Deck
randomness is keyed by candidate-neutral ``FormalSeedCohort`` objects so every
eligible candidate/checkpoint/mode receives the same paired deals.

The in-process authority sentinels below detect accidental construction/copy
paths.  They are not cryptographic signatures and must not be described as an
external trust boundary; the Bitcoin/drand receipts and raw replay/resource
evidence provide that boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from itertools import combinations, product
from typing import Any, Mapping, Sequence

from .deal_generator import DEAL_GENERATOR_ALGORITHM_DIGEST
from .evaluation import (
    ArtifactIdentity,
    EvaluationStratum,
    FormalEvaluationPlan,
    MatchObservation,
    PairedBlock,
    ResourceProfile,
    RetryLedger,
    aggregate_blocks,
    holm_adjust,
)
from .seeds import FinalEvaluationPlan, FormalSeedCohort, VerifiedBeacon


Digest = str

ROUTE_IDS = ("A1", "A2", "B")
CHECKPOINT_IDS = ("equal-offline-compute", "best-validated")
COMPARISON_MODES = ("controlled", "best-of-route")
FORMAL_BUDGETS_MS = (250, 5_000, 20_000, 50_000)
FIXED_OPPONENT_ROLES = ("current-pool", "stable-anchor", "nemesis-exploit")
HELDOUT_ROLE = "final-heldout"
HELDOUT_SLOT_IDS = ("heldout-0", "heldout-1", "heldout-2", "heldout-3")
HELDOUT_SELECTION_COUNT = len(HELDOUT_SLOT_IDS)
HELDOUT_RANKING_RULE = "FinalEvaluationPlan.rank_opponents:hmac-sha256:v1"
DIRECT_PAIRED_BLOCKS = 400
EXTERNAL_PAIRED_BLOCKS = 100
ABLATION_PAIRED_BLOCKS = 100
BOOTSTRAP_SAMPLES = 10_000
SIGN_FLIP_SAMPLES = 100_000
MAX_INFRASTRUCTURE_RETRIES_PER_LEG = 2
FAMILY_ALPHA = 0.05
STOPPING_MODE = "fixed-complete-block-set"
ANALYSIS_ESTIMAND = "mean-two-leg-paired-block-score-difference"
ANALYSIS_ALTERNATIVE = "two-sided"
ANALYSIS_CLUSTER = "entire-two-leg-paired-block"
ANALYSIS_INTERVAL = "paired-percentile-bootstrap-union-hoeffding"
ANALYSIS_P_VALUE = "paired-sign-flip-plus-one"

COMMON_ABLATION_IDS = (
    "blueprint-only",
    "no-online-search",
    "no-neural-leaf-value",
    "no-dynamic-sizing",
    "no-cross-hand-model",
    "no-70-hand-controller",
)
ROUTE_SPECIFIC_ABLATION_IDS: Mapping[str, tuple[str, ...]] = {
    "A1": (
        "no-policy-warm-start",
        "pbs-value-training-variant",
        "search-iteration-count",
    ),
    "A2": (
        "plain-resolve",
        "safe-resolve",
        "off-tree-disabled",
        "action-abstraction-granularity",
    ),
    "B": (
        "mccfr-vs-dcfr",
        "street-search-depth",
        "leaf-neural-vs-exact-vs-rollout",
        "posterior-disabled",
        "safe-exploitation-disabled",
        "match-controller-objective-variant",
    ),
}
ABLATION_STATUSES = ("materialized", "not-applicable")

_MATRIX_AUTHORITY = object()
_SELECTION_AUTHORITY = object()
_DIAGNOSTIC_SELECTION_AUTHORITY = object()
_PROJECTION_AUTHORITY = object()
_DIAGNOSTIC_PROJECTION_AUTHORITY = object()
_PLAN_BRIDGE_AUTHORITY = object()
_DIAGNOSTIC_PLAN_BRIDGE_AUTHORITY = object()
_LEDGER_AUTHORITY = object()
_FORMAL_CELL_RESULT_AUTHORITY = object()

CHECKPOINT_FREEZE_DECLARATION_ONLY = "development-declaration-only"
CHECKPOINT_FREEZE_EXTERNAL_AUTHORITY = "fixed-external-checkpoint-freezer-v1"
FORMAL_CHECKPOINT_FREEZE_AVAILABLE = False
FORMAL_CHECKPOINT_FREEZE_UNAVAILABLE_REASON = (
    "formal checkpoint freeze is unavailable: equal-compute accounting, "
    "validation-only selection, and no-heldout-access are currently local "
    "declarations; no fixed external authority has signed the training/resource "
    "ledger and heldout-access audit"
)


def _digest(value: str, name: str) -> Digest:
    if not isinstance(value, str) or value != value.lower():
        raise ValueError(f"{name} must be lowercase hexadecimal")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be lowercase hexadecimal") from exc
    if len(decoded) != 32:
        raise ValueError(f"{name} must be a 32-byte digest")
    return value


def _payload_digest(payload: Any) -> Digest:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _identity_set_digest(values: Sequence[Digest]) -> Digest:
    normalized = tuple(sorted(_digest(value, "identity-set member") for value in values))
    if len(set(normalized)) != len(normalized):
        raise ValueError("identity set contains duplicates")
    return _payload_digest({"schema": "identity-set-v1", "members": normalized})


def _artifact_identity_payload(artifact: ArtifactIdentity) -> dict[str, str]:
    return {
        "identity_digest": artifact.identity_digest(),
        "sealed_tree_manifest_digest": artifact.sealed_tree_manifest_digest,
        "launch_contract_digest": artifact.launch_contract_digest,
        "launch_command_digest": artifact.launch_command_digest,
        "base_environment_digest": artifact.base_environment_digest,
        "model_digest": artifact.model_digest,
        "config_digest": artifact.config_digest,
        "action_set_digest": artifact.action_set_digest,
        "dependency_digest": artifact.dependency_digest,
        "runtime_digest": artifact.runtime_digest,
    }


def _route_order(route_id: str) -> int:
    try:
        return ROUTE_IDS.index(route_id)
    except ValueError as exc:
        raise ValueError(f"unknown research route: {route_id}") from exc


def _mandatory_ablation_ids(route_id: str) -> tuple[str, ...]:
    _route_order(route_id)
    return COMMON_ABLATION_IDS + ROUTE_SPECIFIC_ABLATION_IDS[route_id]


@dataclass(frozen=True, slots=True)
class MainArtifact:
    checkpoint: str
    comparison_mode: str
    artifact: ArtifactIdentity

    def __post_init__(self) -> None:
        if self.checkpoint not in CHECKPOINT_IDS:
            raise ValueError("unknown main-artifact checkpoint")
        if self.comparison_mode not in COMPARISON_MODES:
            raise ValueError("unknown main-artifact comparison mode")
        if not isinstance(self.artifact, ArtifactIdentity):
            raise ValueError("main artifact must carry an ArtifactIdentity")

    @property
    def key(self) -> tuple[str, str]:
        return (self.checkpoint, self.comparison_mode)

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "checkpoint": self.checkpoint,
                "comparison_mode": self.comparison_mode,
                "artifact": _artifact_identity_payload(self.artifact),
            }
        )


@dataclass(frozen=True, slots=True)
class RouteArtifacts:
    """Exactly four main artifacts for one route."""

    route_id: str
    main_artifacts: tuple[MainArtifact, ...]

    def __post_init__(self) -> None:
        _route_order(self.route_id)
        items = tuple(sorted(self.main_artifacts, key=lambda item: item.key))
        expected = set(product(CHECKPOINT_IDS, COMPARISON_MODES))
        keys = {item.key for item in items}
        if len(items) != 4 or keys != expected:
            raise ValueError("each route requires exactly four main checkpoint/mode artifacts")
        identities = tuple(item.artifact.identity_digest() for item in items)
        if len(set(identities)) != 4:
            raise ValueError("the four main route artifacts must be content-distinct")
        controlled = {
            item.artifact.action_set_digest
            for item in items
            if item.comparison_mode == "controlled"
        }
        if len(controlled) != 1:
            raise ValueError("one route's controlled checkpoints must share one action set")
        object.__setattr__(self, "main_artifacts", items)

    def artifact_for(self, checkpoint: str, comparison_mode: str) -> ArtifactIdentity:
        matches = [
            item.artifact
            for item in self.main_artifacts
            if item.key == (checkpoint, comparison_mode)
        ]
        if len(matches) != 1:
            raise KeyError((checkpoint, comparison_mode))
        return matches[0]

    def identity_for(self, checkpoint: str, comparison_mode: str) -> Digest:
        return self.artifact_for(checkpoint, comparison_mode).identity_digest()

    @property
    def identity_digests(self) -> tuple[Digest, ...]:
        return tuple(item.artifact.identity_digest() for item in self.main_artifacts)

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "route_id": self.route_id,
                "main_artifact_digests": tuple(item.digest() for item in self.main_artifacts),
            }
        )


@dataclass(frozen=True, slots=True)
class CheckpointFreezeReceipt:
    """Content declaration for one route/checkpoint freeze.

    The legacy fields remain useful for deterministic matrix construction, but
    they are *claims*, not evidence.  Formal authority additionally requires a
    fixed external checkpoint-freezer receipt.  There is intentionally no local
    minting factory while that authority is not installed: filling the digest
    fields, the validation-only string, or the no-heldout boolean cannot turn a
    declaration into formal evidence.
    """

    route_id: str
    checkpoint: str
    controlled_identity_digest: Digest
    best_of_route_identity_digest: Digest
    offline_compute_budget_digest: Digest
    training_seed_manifest_digest: Digest
    training_data_manifest_digest: Digest
    training_resource_receipt_digest: Digest
    validation_selection_digest: Digest
    selected_on: str = "validation-only"
    final_heldout_accessed: bool = False
    authority_kind: str = CHECKPOINT_FREEZE_DECLARATION_ONLY
    authority_receipt_digest: Digest | None = None
    _authority_guard: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _route_order(self.route_id)
        if self.checkpoint not in CHECKPOINT_IDS:
            raise ValueError("unknown checkpoint-freeze checkpoint")
        if self.selected_on != "validation-only":
            raise ValueError("checkpoint selection must use validation only")
        if type(self.final_heldout_accessed) is not bool or self.final_heldout_accessed:
            raise ValueError("a frozen checkpoint may not have accessed final heldout")
        if self.authority_kind not in {
            CHECKPOINT_FREEZE_DECLARATION_ONLY,
            CHECKPOINT_FREEZE_EXTERNAL_AUTHORITY,
        }:
            raise ValueError("unknown checkpoint-freeze authority kind")
        if self.authority_receipt_digest is not None:
            object.__setattr__(
                self,
                "authority_receipt_digest",
                _digest(
                    self.authority_receipt_digest,
                    "checkpoint-freeze authority receipt",
                ),
            )
        if (
            self.authority_kind == CHECKPOINT_FREEZE_DECLARATION_ONLY
            and self.authority_receipt_digest is not None
        ):
            raise ValueError(
                "a declaration-only checkpoint freeze cannot carry an authority receipt"
            )
        for name in (
            "controlled_identity_digest",
            "best_of_route_identity_digest",
            "offline_compute_budget_digest",
            "training_seed_manifest_digest",
            "training_data_manifest_digest",
            "training_resource_receipt_digest",
            "validation_selection_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if self.controlled_identity_digest == self.best_of_route_identity_digest:
            raise ValueError("controlled and best-of-route checkpoint artifacts must differ")

    @property
    def key(self) -> tuple[str, str]:
        return (self.route_id, self.checkpoint)

    def assert_matches(self, route: RouteArtifacts) -> None:
        if route.route_id != self.route_id:
            raise ValueError("checkpoint receipt belongs to another route")
        expected = (
            route.identity_for(self.checkpoint, "controlled"),
            route.identity_for(self.checkpoint, "best-of-route"),
        )
        if expected != (
            self.controlled_identity_digest,
            self.best_of_route_identity_digest,
        ):
            raise ValueError("checkpoint receipt does not bind the route artifacts")

    def digest(self) -> Digest:
        payload = asdict(self)
        payload.pop("_authority_guard", None)
        return _payload_digest(payload)

    def _assert_formal_authority(self) -> None:
        guard = self._authority_guard
        if (
            not FORMAL_CHECKPOINT_FREEZE_AVAILABLE
            or self.authority_kind != CHECKPOINT_FREEZE_EXTERNAL_AUTHORITY
            or self.authority_receipt_digest is None
            or not callable(guard)
            or guard(self) is not True
        ):
            raise ValueError(FORMAL_CHECKPOINT_FREEZE_UNAVAILABLE_REASON)


@dataclass(frozen=True, slots=True)
class AblationRegistration:
    route_id: str
    ablation_id: str
    status: str
    rationale: str
    baseline_checkpoint: str = "best-validated"
    baseline_comparison_mode: str = "best-of-route"
    ablated_artifact: ArtifactIdentity | None = None

    def __post_init__(self) -> None:
        _route_order(self.route_id)
        if self.ablation_id not in _mandatory_ablation_ids(self.route_id):
            raise ValueError("ablation ID is not mandatory for this route")
        if self.status not in ABLATION_STATUSES:
            raise ValueError("unknown ablation status")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("every ablation requires a non-empty rationale")
        if self.baseline_checkpoint not in CHECKPOINT_IDS:
            raise ValueError("ablation baseline checkpoint is unknown")
        if self.baseline_comparison_mode not in COMPARISON_MODES:
            raise ValueError("ablation baseline comparison mode is unknown")
        if self.status == "materialized" and not isinstance(
            self.ablated_artifact, ArtifactIdentity
        ):
            raise ValueError("materialized ablation requires an artifact")
        if self.status == "not-applicable" and self.ablated_artifact is not None:
            raise ValueError("not-applicable ablation must not smuggle an artifact")

    @property
    def key(self) -> tuple[str, str]:
        return (self.route_id, self.ablation_id)

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "route_id": self.route_id,
                "ablation_id": self.ablation_id,
                "status": self.status,
                "rationale": self.rationale,
                "baseline_checkpoint": self.baseline_checkpoint,
                "baseline_comparison_mode": self.baseline_comparison_mode,
                "ablated_artifact": (
                    None
                    if self.ablated_artifact is None
                    else _artifact_identity_payload(self.ablated_artifact)
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class MandatoryAblationRegistry:
    entries: tuple[AblationRegistration, ...]

    def __post_init__(self) -> None:
        entries = tuple(sorted(self.entries, key=lambda item: item.key))
        expected = {
            (route_id, ablation_id)
            for route_id in ROUTE_IDS
            for ablation_id in _mandatory_ablation_ids(route_id)
        }
        keys = {item.key for item in entries}
        if len(entries) != len(expected) or keys != expected:
            raise ValueError("ablation registry must explicitly cover every mandatory route key")
        materialized = [
            item.ablated_artifact.identity_digest()
            for item in entries
            if item.ablated_artifact is not None
        ]
        if len(set(materialized)) != len(materialized):
            raise ValueError("materialized ablation artifacts must be content-distinct")
        object.__setattr__(self, "entries", entries)

    @property
    def materialized(self) -> tuple[AblationRegistration, ...]:
        return tuple(item for item in self.entries if item.status == "materialized")

    @property
    def not_applicable(self) -> tuple[AblationRegistration, ...]:
        return tuple(item for item in self.entries if item.status == "not-applicable")

    def entry(self, route_id: str, ablation_id: str) -> AblationRegistration:
        matches = [item for item in self.entries if item.key == (route_id, ablation_id)]
        if len(matches) != 1:
            raise KeyError((route_id, ablation_id))
        return matches[0]

    def digest(self) -> Digest:
        return _payload_digest(tuple(item.digest() for item in self.entries))


@dataclass(frozen=True, slots=True)
class FrozenOpponentFamily:
    role: str
    members: tuple[ArtifactIdentity, ...]

    def __post_init__(self) -> None:
        if self.role not in FIXED_OPPONENT_ROLES:
            raise ValueError("unknown fixed opponent role")
        members = tuple(sorted(self.members, key=lambda item: item.identity_digest()))
        identities = tuple(item.identity_digest() for item in members)
        if not members or len(set(identities)) != len(identities):
            raise ValueError("fixed opponent family must be non-empty and content-unique")
        if self.role == "stable-anchor" and len(members) != 1:
            raise ValueError("stable-anchor family must contain exactly one artifact")
        object.__setattr__(self, "members", members)

    @property
    def identity_digests(self) -> tuple[Digest, ...]:
        return tuple(item.identity_digest() for item in self.members)

    def manifest_digest(self) -> Digest:
        return _payload_digest(
            {"role": self.role, "member_identity_digests": self.identity_digests}
        )


@dataclass(frozen=True, slots=True)
class OpponentSplitFreeze:
    train_identity_digests: tuple[Digest, ...]
    dev_identity_digests: tuple[Digest, ...]
    validation_identity_digests: tuple[Digest, ...]

    def __post_init__(self) -> None:
        normalized: list[tuple[Digest, ...]] = []
        for name in (
            "train_identity_digests",
            "dev_identity_digests",
            "validation_identity_digests",
        ):
            values = tuple(sorted(_digest(value, name) for value in getattr(self, name)))
            if not values or len(set(values)) != len(values):
                raise ValueError(f"{name} must be non-empty and content-unique")
            normalized.append(values)
            object.__setattr__(self, name, values)
        if any(set(left) & set(right) for left, right in combinations(normalized, 2)):
            raise ValueError("train/dev/validation opponent identities must be disjoint")

    @property
    def all_identity_digests(self) -> tuple[Digest, ...]:
        return (
            self.train_identity_digests
            + self.dev_identity_digests
            + self.validation_identity_digests
        )

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))


def _heldout_commitment_digest(salt_hex: str, identities: Sequence[Digest]) -> Digest:
    try:
        salt = bytes.fromhex(salt_hex)
    except (TypeError, ValueError) as exc:
        raise ValueError("heldout salt must be lowercase hexadecimal") from exc
    if salt_hex != salt_hex.lower() or len(salt) < 32:
        raise ValueError("heldout salt must contain at least 32 random bytes")
    normalized = tuple(sorted(_digest(value, "heldout identity") for value in identities))
    if len(set(normalized)) != len(normalized):
        raise ValueError("heldout reveal contains duplicate identities")
    payload = json.dumps(
        {"schema": "heldout-salted-commitment-v1", "identities": normalized},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"pok-heldout-v1\0" + salt + b"\0" + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class HeldoutPrecommitment:
    salted_commitment_digest: Digest
    universe_size: int
    known_nonheldout_identity_set_digest: Digest
    selection_count: int = HELDOUT_SELECTION_COUNT
    minimum_salt_bytes: int = 32
    ranking_rule: str = HELDOUT_RANKING_RULE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "salted_commitment_digest",
            _digest(self.salted_commitment_digest, "heldout salted commitment"),
        )
        object.__setattr__(
            self,
            "known_nonheldout_identity_set_digest",
            _digest(
                self.known_nonheldout_identity_set_digest,
                "known nonheldout identity set",
            ),
        )
        if type(self.universe_size) is not int or self.universe_size < self.selection_count + 1:
            raise ValueError("heldout universe must be larger than the selected subset")
        if self.selection_count != HELDOUT_SELECTION_COUNT:
            raise ValueError("formal heldout selection count is exactly four")
        if self.minimum_salt_bytes < 32:
            raise ValueError("heldout commitment salt floor is 32 bytes")
        if self.ranking_rule != HELDOUT_RANKING_RULE:
            raise ValueError("heldout ranking rule differs from the frozen contract")

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class HeldoutReveal:
    salt_hex: str
    universe: tuple[ArtifactIdentity, ...]

    def __post_init__(self) -> None:
        # The helper validates salt format and identities without retaining a
        # second derived representation.
        identities = tuple(item.identity_digest() for item in self.universe)
        _heldout_commitment_digest(self.salt_hex, identities)
        universe = tuple(sorted(self.universe, key=lambda item: item.identity_digest()))
        object.__setattr__(self, "universe", universe)

    @property
    def identity_digests(self) -> tuple[Digest, ...]:
        return tuple(item.identity_digest() for item in self.universe)

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "salt_hex": self.salt_hex,
                "artifacts": tuple(_artifact_identity_payload(item) for item in self.universe),
            }
        )

    def verify(
        self,
        precommitment: HeldoutPrecommitment,
        known_nonheldout_identity_digests: Sequence[Digest],
    ) -> None:
        known_digest = _identity_set_digest(known_nonheldout_identity_digests)
        if known_digest != precommitment.known_nonheldout_identity_set_digest:
            raise ValueError("heldout reveal uses a different frozen nonheldout identity set")
        if len(self.universe) != precommitment.universe_size:
            raise ValueError("heldout reveal universe size differs from its precommitment")
        if set(self.identity_digests) & set(known_nonheldout_identity_digests):
            raise ValueError("heldout identities overlap train/dev/validation/fixed/research data")
        expected = _heldout_commitment_digest(self.salt_hex, self.identity_digests)
        if expected != precommitment.salted_commitment_digest:
            raise ValueError("heldout salt/reveal does not open the frozen commitment")


def create_heldout_precommitment(
    reveal: HeldoutReveal,
    known_nonheldout_identity_digests: Sequence[Digest],
) -> HeldoutPrecommitment:
    known = tuple(known_nonheldout_identity_digests)
    if set(reveal.identity_digests) & set(known):
        raise ValueError("heldout identities must be disjoint before commitment")
    return HeldoutPrecommitment(
        salted_commitment_digest=_heldout_commitment_digest(
            reveal.salt_hex, reveal.identity_digests
        ),
        universe_size=len(reveal.universe),
        known_nonheldout_identity_set_digest=_identity_set_digest(known),
    )


@dataclass(frozen=True, slots=True)
class MatrixSharedContracts:
    common_contract_tree_digest: Digest
    rules_digest: Digest
    harness_digest: Digest
    deal_generator_digest: Digest
    evaluation_contract_digest: Digest
    final_randomness_contract_digest: Digest
    stopping_rule_digest: Digest
    retry_policy_digest: Digest
    analysis_code_digest: Digest
    replay_verifier_digest: Digest
    oracle_fixture_digest: Digest
    infrastructure_monitor_digest: Digest
    heldout_commitment_contract_digest: Digest
    ablation_registry_contract_digest: Digest
    result_ledger_contract_digest: Digest

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if self.deal_generator_digest != DEAL_GENERATOR_ALGORITHM_DIGEST:
            raise ValueError("matrix must use the frozen common deal generator")

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class BudgetProfile:
    budget_ms: int
    profile: ResourceProfile

    def __post_init__(self) -> None:
        if self.budget_ms not in FORMAL_BUDGETS_MS:
            raise ValueError("budget profile is outside the four formal cells")
        if self.profile.decision_budget_ms != self.budget_ms:
            raise ValueError("budget profile and resource profile disagree")

    def digest(self) -> Digest:
        return _payload_digest(
            {"budget_ms": self.budget_ms, "resource_profile_digest": self.profile.digest()}
        )


@dataclass(frozen=True, slots=True)
class MatrixCellKey:
    kind: str
    budget_ms: int
    comparison_mode: str
    focal_route: str
    checkpoint: str | None = None
    peer_route: str | None = None
    opponent_role: str | None = None
    opponent_identity_digest: Digest | None = None
    heldout_slot: str | None = None
    ablation_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"direct-h2h", "fixed-opponent", "heldout-slot", "ablation"}:
            raise ValueError("unknown matrix cell kind")
        if self.budget_ms not in FORMAL_BUDGETS_MS:
            raise ValueError("matrix cell uses an unregistered budget")
        if self.comparison_mode not in COMPARISON_MODES:
            raise ValueError("matrix cell uses an unknown comparison mode")
        _route_order(self.focal_route)
        if self.kind != "ablation" and self.checkpoint not in CHECKPOINT_IDS:
            raise ValueError("main matrix cell requires a frozen checkpoint")
        if self.kind == "direct-h2h":
            if self.peer_route is None or _route_order(self.focal_route) >= _route_order(self.peer_route):
                raise ValueError("direct H2H route pair must be canonical and unordered")
            if any(
                value is not None
                for value in (
                    self.opponent_role,
                    self.opponent_identity_digest,
                    self.heldout_slot,
                    self.ablation_id,
                )
            ):
                raise ValueError("direct H2H cell contains external dimensions")
        elif self.kind == "fixed-opponent":
            if self.opponent_role not in FIXED_OPPONENT_ROLES:
                raise ValueError("fixed cell has an unknown opponent role")
            object.__setattr__(
                self,
                "opponent_identity_digest",
                _digest(self.opponent_identity_digest, "fixed opponent identity"),
            )
            if any(value is not None for value in (self.peer_route, self.heldout_slot, self.ablation_id)):
                raise ValueError("fixed cell contains unrelated dimensions")
        elif self.kind == "heldout-slot":
            if self.heldout_slot not in HELDOUT_SLOT_IDS:
                raise ValueError("heldout cell has an unknown opaque slot")
            if any(
                value is not None
                for value in (
                    self.peer_route,
                    self.opponent_role,
                    self.opponent_identity_digest,
                    self.ablation_id,
                )
            ):
                raise ValueError("heldout cell leaks an identity or unrelated dimension")
        else:
            if self.ablation_id not in _mandatory_ablation_ids(self.focal_route):
                raise ValueError("ablation cell has an unknown registry key")
            if self.checkpoint is not None:
                raise ValueError("ablation baseline is frozen in its registry entry")
            if any(
                value is not None
                for value in (
                    self.peer_route,
                    self.opponent_role,
                    self.opponent_identity_digest,
                    self.heldout_slot,
                )
            ):
                raise ValueError("ablation cell contains external dimensions")

    def payload(self) -> dict[str, object]:
        return asdict(self)

    def sort_key(self) -> tuple[object, ...]:
        return (
            self.kind,
            _route_order(self.focal_route),
            -1 if self.peer_route is None else _route_order(self.peer_route),
            self.checkpoint or "",
            self.comparison_mode,
            self.opponent_role or "",
            self.opponent_identity_digest or "",
            self.heldout_slot or "",
            self.ablation_id or "",
            self.budget_ms,
        )

    def digest(self) -> Digest:
        return _payload_digest(self.payload())


@dataclass(frozen=True, slots=True)
class PlannedStratumTemplate:
    key: MatrixCellKey
    focal_identity_digest: Digest
    counterparty_identity_digest: Digest | None
    opponent_family_template_digest: Digest
    split: str
    resource_profile_digest: Digest
    seed_cohort: FormalSeedCohort
    paired_block_count: int
    stopping_rule_digest: Digest
    retry_policy_digest: Digest
    analysis_code_digest: Digest
    bootstrap_samples: int
    sign_flip_samples: int
    max_infrastructure_retries_per_leg: int
    hypothesis_id: Digest

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "focal_identity_digest",
            _digest(self.focal_identity_digest, "template focal identity"),
        )
        if self.counterparty_identity_digest is not None:
            object.__setattr__(
                self,
                "counterparty_identity_digest",
                _digest(self.counterparty_identity_digest, "template counterparty identity"),
            )
            if self.counterparty_identity_digest == self.focal_identity_digest:
                raise ValueError("template cannot compare an artifact with itself")
        elif self.key.kind != "heldout-slot":
            raise ValueError("only heldout templates may defer counterparty identity")
        if self.split not in {
            "direct-h2h",
            "current-pool",
            "stable-anchor",
            "nemesis-exploit",
            "final-heldout",
            "ablation",
        }:
            raise ValueError("template uses an unknown split")
        if self.key.kind == "heldout-slot" and self.split != "final-heldout":
            raise ValueError("opaque heldout template has the wrong split")
        if self.key.kind == "ablation" and self.split != "ablation":
            raise ValueError("ablation template has the wrong split")
        object.__setattr__(
            self,
            "opponent_family_template_digest",
            _digest(self.opponent_family_template_digest, "opponent family template"),
        )
        object.__setattr__(
            self,
            "resource_profile_digest",
            _digest(self.resource_profile_digest, "template resource profile"),
        )
        if self.seed_cohort.budget_ms != self.key.budget_ms:
            raise ValueError("template and seed cohort budgets differ")
        if self.seed_cohort.paired_block_count != self.paired_block_count:
            raise ValueError("template and seed cohort block counts differ")
        if self.paired_block_count not in {
            DIRECT_PAIRED_BLOCKS,
            EXTERNAL_PAIRED_BLOCKS,
            ABLATION_PAIRED_BLOCKS,
        }:
            raise ValueError("template block count is outside the preregistered floors")
        if self.bootstrap_samples < BOOTSTRAP_SAMPLES:
            raise ValueError("template bootstrap count is below the frozen minimum")
        if self.sign_flip_samples < SIGN_FLIP_SAMPLES:
            raise ValueError("template sign-flip count is below the frozen minimum")
        if self.max_infrastructure_retries_per_leg != MAX_INFRASTRUCTURE_RETRIES_PER_LEG:
            raise ValueError("template retry count differs from the frozen policy")
        for name in (
            "stopping_rule_digest",
            "retry_policy_digest",
            "analysis_code_digest",
            "hypothesis_id",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))

    @property
    def comparison_mode(self) -> str:
        return self.key.comparison_mode

    @property
    def seed_cohort_digest(self) -> Digest:
        return self.seed_cohort.digest()

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "key": self.key.payload(),
                "focal_identity_digest": self.focal_identity_digest,
                "counterparty_identity_digest": self.counterparty_identity_digest,
                "opponent_family_template_digest": self.opponent_family_template_digest,
                "split": self.split,
                "resource_profile_digest": self.resource_profile_digest,
                "seed_cohort_digest": self.seed_cohort.digest(),
                "paired_block_count": self.paired_block_count,
                "stopping_rule_digest": self.stopping_rule_digest,
                "retry_policy_digest": self.retry_policy_digest,
                "analysis_code_digest": self.analysis_code_digest,
                "bootstrap_samples": self.bootstrap_samples,
                "sign_flip_samples": self.sign_flip_samples,
                "max_infrastructure_retries_per_leg": self.max_infrastructure_retries_per_leg,
                "hypothesis_id": self.hypothesis_id,
            }
        )


@dataclass(frozen=True, slots=True)
class PairwiseHypothesisContract:
    """One promotion/ablation claim over paired-block cluster scores."""

    kind: str
    left_route: str
    right_route: str | None
    left_template_digest: Digest
    right_template_digest: Digest | None
    left_identity_digest: Digest
    right_identity_digest: Digest
    seed_cohort_digest: Digest
    checkpoint: str | None
    comparison_mode: str
    budget_ms: int
    estimand: str
    alternative: str
    hypothesis_id: Digest

    def __post_init__(self) -> None:
        if self.kind not in {
            "direct-h2h",
            "external-paired-difference",
            "ablation",
        }:
            raise ValueError("unknown pairwise hypothesis kind")
        _route_order(self.left_route)
        if self.kind == "external-paired-difference":
            if self.right_route is None or _route_order(self.left_route) >= _route_order(
                self.right_route
            ):
                raise ValueError("external paired hypothesis routes must be canonical")
            if self.right_template_digest is None:
                raise ValueError("external paired hypothesis requires two result cells")
        elif self.right_template_digest is not None:
            raise ValueError("direct/ablation hypotheses use one two-player result cell")
        if self.checkpoint is not None and self.checkpoint not in CHECKPOINT_IDS:
            raise ValueError("pairwise hypothesis has an unknown checkpoint")
        if self.comparison_mode not in COMPARISON_MODES:
            raise ValueError("pairwise hypothesis has an unknown mode")
        if self.budget_ms not in FORMAL_BUDGETS_MS:
            raise ValueError("pairwise hypothesis has an unknown budget")
        if self.estimand != ANALYSIS_ESTIMAND or self.alternative != ANALYSIS_ALTERNATIVE:
            raise ValueError("pairwise hypothesis changed the frozen estimand")
        for name in (
            "left_template_digest",
            "left_identity_digest",
            "right_identity_digest",
            "seed_cohort_digest",
            "hypothesis_id",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if self.right_template_digest is not None:
            object.__setattr__(
                self,
                "right_template_digest",
                _digest(self.right_template_digest, "right template digest"),
            )
        if self.left_identity_digest == self.right_identity_digest:
            raise ValueError("pairwise hypothesis requires distinct identities")
        if self.hypothesis_id != self.expected_hypothesis_id():
            raise ValueError("pairwise hypothesis ID is not its canonical commitment")

    def payload_without_id(self) -> dict[str, object]:
        return {
            "schema": "formal-pairwise-hypothesis-v1",
            "kind": self.kind,
            "left_route": self.left_route,
            "right_route": self.right_route,
            "left_template_digest": self.left_template_digest,
            "right_template_digest": self.right_template_digest,
            "left_identity_digest": self.left_identity_digest,
            "right_identity_digest": self.right_identity_digest,
            "seed_cohort_digest": self.seed_cohort_digest,
            "checkpoint": self.checkpoint,
            "comparison_mode": self.comparison_mode,
            "budget_ms": self.budget_ms,
            "estimand": self.estimand,
            "alternative": self.alternative,
        }

    def expected_hypothesis_id(self) -> Digest:
        return _payload_digest(self.payload_without_id())

    def digest(self) -> Digest:
        return _payload_digest(
            {**self.payload_without_id(), "hypothesis_id": self.hypothesis_id}
        )


def _pairwise_hypothesis(
    *,
    kind: str,
    left: PlannedStratumTemplate,
    right: PlannedStratumTemplate | None,
) -> PairwiseHypothesisContract:
    if right is None:
        if left.counterparty_identity_digest is None:
            raise ValueError("single-cell pairwise hypothesis has no counterparty")
        right_identity = left.counterparty_identity_digest
        right_route = left.key.peer_route if left.key.kind == "direct-h2h" else None
    else:
        if left.seed_cohort_digest != right.seed_cohort_digest:
            raise ValueError("paired hypothesis templates do not share one seed cohort")
        right_identity = right.focal_identity_digest
        right_route = right.key.focal_route
    provisional = {
        "schema": "formal-pairwise-hypothesis-v1",
        "kind": kind,
        "left_route": left.key.focal_route,
        "right_route": right_route,
        "left_template_digest": left.digest(),
        "right_template_digest": None if right is None else right.digest(),
        "left_identity_digest": left.focal_identity_digest,
        "right_identity_digest": right_identity,
        "seed_cohort_digest": left.seed_cohort_digest,
        "checkpoint": left.key.checkpoint,
        "comparison_mode": left.key.comparison_mode,
        "budget_ms": left.key.budget_ms,
        "estimand": ANALYSIS_ESTIMAND,
        "alternative": ANALYSIS_ALTERNATIVE,
    }
    return PairwiseHypothesisContract(
        kind=kind,
        left_route=left.key.focal_route,
        right_route=right_route,
        left_template_digest=left.digest(),
        right_template_digest=None if right is None else right.digest(),
        left_identity_digest=left.focal_identity_digest,
        right_identity_digest=right_identity,
        seed_cohort_digest=left.seed_cohort_digest,
        checkpoint=left.key.checkpoint,
        comparison_mode=left.key.comparison_mode,
        budget_ms=left.key.budget_ms,
        estimand=ANALYSIS_ESTIMAND,
        alternative=ANALYSIS_ALTERNATIVE,
        hypothesis_id=_payload_digest(provisional),
    )


def _build_pairwise_hypotheses(
    templates: Sequence[PlannedStratumTemplate],
) -> tuple[PairwiseHypothesisContract, ...]:
    hypotheses: list[PairwiseHypothesisContract] = []
    external_groups: dict[tuple[object, ...], dict[str, PlannedStratumTemplate]] = {}
    for template in templates:
        key = template.key
        if key.kind == "direct-h2h":
            hypotheses.append(_pairwise_hypothesis(kind="direct-h2h", left=template, right=None))
        elif key.kind == "ablation":
            hypotheses.append(_pairwise_hypothesis(kind="ablation", left=template, right=None))
        else:
            group_key = (
                key.kind,
                key.checkpoint,
                key.comparison_mode,
                key.budget_ms,
                key.opponent_role,
                key.opponent_identity_digest,
                key.heldout_slot,
            )
            external_groups.setdefault(group_key, {})[key.focal_route] = template
    for group in external_groups.values():
        if set(group) != set(ROUTE_IDS):
            raise ValueError("external pairwise hypothesis group lacks one research route")
        for left_route, right_route in combinations(ROUTE_IDS, 2):
            hypotheses.append(
                _pairwise_hypothesis(
                    kind="external-paired-difference",
                    left=group[left_route],
                    right=group[right_route],
                )
            )
    return tuple(sorted(hypotheses, key=lambda item: item.hypothesis_id))


@dataclass(frozen=True, slots=True)
class HolmFamilyContract:
    hypothesis_ids: tuple[Digest, ...]
    method: str = "holm"
    alpha: float = FAMILY_ALPHA
    tie_breaker: str = "hypothesis-id"
    incomplete_family_policy: str = "fail-closed"

    def __post_init__(self) -> None:
        hypotheses = tuple(sorted(_digest(value, "Holm hypothesis") for value in self.hypothesis_ids))
        if not hypotheses or len(set(hypotheses)) != len(hypotheses):
            raise ValueError("Holm family must be non-empty and unique")
        if self.method != "holm" or self.tie_breaker != "hypothesis-id":
            raise ValueError("unsupported multiplicity contract")
        if self.incomplete_family_policy != "fail-closed":
            raise ValueError("formal multiplicity family must fail closed")
        if not 0.0 < self.alpha < 1.0:
            raise ValueError("Holm alpha must be in (0,1)")
        object.__setattr__(self, "hypothesis_ids", hypotheses)

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))

    def adjust(self, raw_p_values: Mapping[Digest, float]) -> dict[Digest, float]:
        if set(raw_p_values) != set(self.hypothesis_ids):
            raise ValueError("raw p-values must exactly cover the complete Holm family")
        # ``holm_adjust`` has the desired deterministic lexical tie behavior.
        adjusted = holm_adjust(dict(raw_p_values))
        return {hypothesis: adjusted[hypothesis] for hypothesis in self.hypothesis_ids}


def _known_nonheldout_identities(
    routes: Sequence[RouteArtifacts],
    ablations: MandatoryAblationRegistry,
    families: Sequence[FrozenOpponentFamily],
    splits: OpponentSplitFreeze,
) -> tuple[Digest, ...]:
    values = [identity for route in routes for identity in route.identity_digests]
    values.extend(
        item.ablated_artifact.identity_digest()
        for item in ablations.materialized
        if item.ablated_artifact is not None
    )
    values.extend(identity for family in families for identity in family.identity_digests)
    values.extend(splits.all_identity_digests)
    normalized = tuple(sorted(values))
    if len(set(normalized)) != len(normalized):
        raise ValueError(
            "research, ablation, fixed, and train/dev/validation identities must be disjoint"
        )
    return normalized


def _scope_digest(payload: Mapping[str, object]) -> Digest:
    return _payload_digest({"schema": "formal-seed-counterparty-scope-v1", **payload})


def _template(
    *,
    key: MatrixCellKey,
    focal_identity: Digest,
    counterparty_identity: Digest | None,
    opponent_family_template_digest: Digest,
    split: str,
    profile: BudgetProfile,
    seed_scope_digest: Digest,
    seed_domain: str,
    paired_blocks: int,
    contracts: MatrixSharedContracts,
) -> PlannedStratumTemplate:
    cohort = FormalSeedCohort(
        comparison_domain=seed_domain,
        budget_ms=key.budget_ms,
        counterparty_scope_digest=seed_scope_digest,
        paired_block_count=paired_blocks,
        common_contract_digest=contracts.digest(),
    )
    hypothesis = _payload_digest(
        {
            "schema": "formal-matrix-hypothesis-v2",
            "cell_key_digest": key.digest(),
            "focal_identity_digest": focal_identity,
            "counterparty_identity_digest": counterparty_identity,
            "opponent_family_template_digest": opponent_family_template_digest,
            "seed_cohort_digest": cohort.digest(),
            "estimand": ANALYSIS_ESTIMAND,
            "alternative": ANALYSIS_ALTERNATIVE,
        }
    )
    return PlannedStratumTemplate(
        key=key,
        focal_identity_digest=focal_identity,
        counterparty_identity_digest=counterparty_identity,
        opponent_family_template_digest=opponent_family_template_digest,
        split=split,
        resource_profile_digest=profile.profile.digest(),
        seed_cohort=cohort,
        paired_block_count=paired_blocks,
        stopping_rule_digest=contracts.stopping_rule_digest,
        retry_policy_digest=contracts.retry_policy_digest,
        analysis_code_digest=contracts.analysis_code_digest,
        bootstrap_samples=BOOTSTRAP_SAMPLES,
        sign_flip_samples=SIGN_FLIP_SAMPLES,
        max_infrastructure_retries_per_leg=MAX_INFRASTRUCTURE_RETRIES_PER_LEG,
        hypothesis_id=hypothesis,
    )


def _build_expected_templates(
    *,
    routes: Sequence[RouteArtifacts],
    ablations: MandatoryAblationRegistry,
    families: Sequence[FrozenOpponentFamily],
    heldout: HeldoutPrecommitment,
    budget_profiles: Sequence[BudgetProfile],
    contracts: MatrixSharedContracts,
) -> tuple[PlannedStratumTemplate, ...]:
    by_route = {route.route_id: route for route in routes}
    by_budget = {item.budget_ms: item for item in budget_profiles}
    templates: list[PlannedStratumTemplate] = []

    # One canonical unordered candidate-pair cell per checkpoint/mode/budget.
    for left_route, right_route in combinations(ROUTE_IDS, 2):
        scope = _scope_digest({"kind": "direct-h2h", "routes": (left_route, right_route)})
        family = _payload_digest({"role": "direct-h2h", "routes": (left_route, right_route)})
        for checkpoint, mode, budget in product(
            CHECKPOINT_IDS, COMPARISON_MODES, FORMAL_BUDGETS_MS
        ):
            key = MatrixCellKey(
                kind="direct-h2h",
                budget_ms=budget,
                comparison_mode=mode,
                focal_route=left_route,
                checkpoint=checkpoint,
                peer_route=right_route,
            )
            templates.append(
                _template(
                    key=key,
                    focal_identity=by_route[left_route].identity_for(checkpoint, mode),
                    counterparty_identity=by_route[right_route].identity_for(checkpoint, mode),
                    opponent_family_template_digest=family,
                    split="direct-h2h",
                    profile=by_budget[budget],
                    seed_scope_digest=scope,
                    seed_domain="direct-h2h",
                    paired_blocks=DIRECT_PAIRED_BLOCKS,
                    contracts=contracts,
                )
            )

    # External cells use the same cohort across every route/checkpoint/mode.
    for family in families:
        for opponent in family.members:
            opponent_identity = opponent.identity_digest()
            scope = _scope_digest(
                {
                    "kind": "external-opponent",
                    "role": family.role,
                    "opponent_identity_digest": opponent_identity,
                }
            )
            for route_id, checkpoint, mode, budget in product(
                ROUTE_IDS, CHECKPOINT_IDS, COMPARISON_MODES, FORMAL_BUDGETS_MS
            ):
                key = MatrixCellKey(
                    kind="fixed-opponent",
                    budget_ms=budget,
                    comparison_mode=mode,
                    focal_route=route_id,
                    checkpoint=checkpoint,
                    opponent_role=family.role,
                    opponent_identity_digest=opponent_identity,
                )
                templates.append(
                    _template(
                        key=key,
                        focal_identity=by_route[route_id].identity_for(checkpoint, mode),
                        counterparty_identity=opponent_identity,
                        opponent_family_template_digest=family.manifest_digest(),
                        split=family.role,
                        profile=by_budget[budget],
                        seed_scope_digest=scope,
                        seed_domain="external-opponent",
                        paired_blocks=EXTERNAL_PAIRED_BLOCKS,
                        contracts=contracts,
                    )
                )

    for slot in HELDOUT_SLOT_IDS:
        scope = _scope_digest(
            {"kind": "external-opponent", "role": HELDOUT_ROLE, "opaque_slot": slot}
        )
        for route_id, checkpoint, mode, budget in product(
            ROUTE_IDS, CHECKPOINT_IDS, COMPARISON_MODES, FORMAL_BUDGETS_MS
        ):
            key = MatrixCellKey(
                kind="heldout-slot",
                budget_ms=budget,
                comparison_mode=mode,
                focal_route=route_id,
                checkpoint=checkpoint,
                heldout_slot=slot,
            )
            templates.append(
                _template(
                    key=key,
                    focal_identity=by_route[route_id].identity_for(checkpoint, mode),
                    counterparty_identity=None,
                    opponent_family_template_digest=heldout.digest(),
                    split=HELDOUT_ROLE,
                    profile=by_budget[budget],
                    seed_scope_digest=scope,
                    seed_domain="external-opponent",
                    paired_blocks=EXTERNAL_PAIRED_BLOCKS,
                    contracts=contracts,
                )
            )

    # All applicable ablations receive common deals at every formal budget.
    ablation_scope = _scope_digest({"kind": "ablation", "scope": "all-routes"})
    for registration in ablations.materialized:
        assert registration.ablated_artifact is not None
        baseline = by_route[registration.route_id].artifact_for(
            registration.baseline_checkpoint,
            registration.baseline_comparison_mode,
        )
        for budget in FORMAL_BUDGETS_MS:
            key = MatrixCellKey(
                kind="ablation",
                budget_ms=budget,
                comparison_mode=registration.baseline_comparison_mode,
                focal_route=registration.route_id,
                ablation_id=registration.ablation_id,
            )
            templates.append(
                _template(
                    key=key,
                    focal_identity=baseline.identity_digest(),
                    counterparty_identity=registration.ablated_artifact.identity_digest(),
                    opponent_family_template_digest=ablations.digest(),
                    split="ablation",
                    profile=by_budget[budget],
                    seed_scope_digest=ablation_scope,
                    seed_domain="ablation",
                    paired_blocks=ABLATION_PAIRED_BLOCKS,
                    contracts=contracts,
                )
            )

    return tuple(sorted(templates, key=lambda item: item.key.sort_key()))


@dataclass(frozen=True, slots=True)
class CompleteFormalMatrix:
    routes: tuple[RouteArtifacts, ...]
    checkpoint_freezes: tuple[CheckpointFreezeReceipt, ...]
    ablation_registry: MandatoryAblationRegistry
    fixed_families: tuple[FrozenOpponentFamily, ...]
    opponent_splits: OpponentSplitFreeze
    heldout_precommitment: HeldoutPrecommitment
    budget_profiles: tuple[BudgetProfile, ...]
    contracts: MatrixSharedContracts
    planned_templates: tuple[PlannedStratumTemplate, ...]
    pairwise_hypotheses: tuple[PairwiseHypothesisContract, ...]
    holm_family: HolmFamilyContract
    _formal_token: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._validate_content()

    def __copy__(self) -> "CompleteFormalMatrix":
        return type(self)(
            routes=self.routes,
            checkpoint_freezes=self.checkpoint_freezes,
            ablation_registry=self.ablation_registry,
            fixed_families=self.fixed_families,
            opponent_splits=self.opponent_splits,
            heldout_precommitment=self.heldout_precommitment,
            budget_profiles=self.budget_profiles,
            contracts=self.contracts,
            planned_templates=self.planned_templates,
            pairwise_hypotheses=self.pairwise_hypotheses,
            holm_family=self.holm_family,
        )

    def __deepcopy__(self, memo: dict[int, object]) -> "CompleteFormalMatrix":
        return type(self)(
            routes=copy.deepcopy(self.routes, memo),
            checkpoint_freezes=copy.deepcopy(self.checkpoint_freezes, memo),
            ablation_registry=copy.deepcopy(self.ablation_registry, memo),
            fixed_families=copy.deepcopy(self.fixed_families, memo),
            opponent_splits=copy.deepcopy(self.opponent_splits, memo),
            heldout_precommitment=copy.deepcopy(self.heldout_precommitment, memo),
            budget_profiles=copy.deepcopy(self.budget_profiles, memo),
            contracts=copy.deepcopy(self.contracts, memo),
            planned_templates=copy.deepcopy(self.planned_templates, memo),
            pairwise_hypotheses=copy.deepcopy(self.pairwise_hypotheses, memo),
            holm_family=copy.deepcopy(self.holm_family, memo),
        )

    def _validate_content(self) -> None:
        routes = tuple(sorted(self.routes, key=lambda item: _route_order(item.route_id)))
        if tuple(item.route_id for item in routes) != ROUTE_IDS:
            raise ValueError("formal matrix requires exactly routes A1, A2, and B")
        if len({identity for route in routes for identity in route.identity_digests}) != 12:
            raise ValueError("formal matrix requires twelve unique main artifact identities")
        controlled_sets = {
            route.artifact_for(checkpoint, "controlled").action_set_digest
            for route in routes
            for checkpoint in CHECKPOINT_IDS
        }
        if len(controlled_sets) != 1:
            raise ValueError("all controlled artifacts must share one frozen action set")
        object.__setattr__(self, "routes", routes)

        freezes = tuple(sorted(self.checkpoint_freezes, key=lambda item: item.key))
        expected_freezes = set(product(ROUTE_IDS, CHECKPOINT_IDS))
        if len(freezes) != 6 or {item.key for item in freezes} != expected_freezes:
            raise ValueError("formal matrix requires both checkpoint receipts for all routes")
        by_route = {route.route_id: route for route in routes}
        for receipt in freezes:
            receipt.assert_matches(by_route[receipt.route_id])
        equal_compute = {
            receipt.offline_compute_budget_digest
            for receipt in freezes
            if receipt.checkpoint == "equal-offline-compute"
        }
        if len(equal_compute) != 1:
            raise ValueError("equal-offline-compute checkpoints must share one compute budget")
        object.__setattr__(self, "checkpoint_freezes", freezes)

        fixed = tuple(
            sorted(self.fixed_families, key=lambda item: FIXED_OPPONENT_ROLES.index(item.role))
        )
        if tuple(item.role for item in fixed) != FIXED_OPPONENT_ROLES:
            raise ValueError("formal matrix requires current-pool, stable-anchor, and nemesis families")
        object.__setattr__(self, "fixed_families", fixed)
        known = _known_nonheldout_identities(
            routes, self.ablation_registry, fixed, self.opponent_splits
        )
        if _identity_set_digest(known) != self.heldout_precommitment.known_nonheldout_identity_set_digest:
            raise ValueError("heldout precommitment does not bind the complete nonheldout set")

        budgets = tuple(sorted(self.budget_profiles, key=lambda item: item.budget_ms))
        if tuple(item.budget_ms for item in budgets) != FORMAL_BUDGETS_MS:
            raise ValueError("resource profiles must cover exactly the four formal budgets")
        if len({item.profile.digest() for item in budgets}) != 4:
            raise ValueError("each formal budget requires a distinct bound resource profile")
        object.__setattr__(self, "budget_profiles", budgets)

        expected = _build_expected_templates(
            routes=routes,
            ablations=self.ablation_registry,
            families=fixed,
            heldout=self.heldout_precommitment,
            budget_profiles=budgets,
            contracts=self.contracts,
        )
        actual = tuple(sorted(self.planned_templates, key=lambda item: item.key.sort_key()))
        if tuple(item.digest() for item in actual) != tuple(item.digest() for item in expected):
            raise ValueError("planned templates differ from the exact complete formal matrix")
        if len({item.digest() for item in actual}) != len(actual):
            raise ValueError("formal matrix contains duplicate planned templates")
        object.__setattr__(self, "planned_templates", actual)
        expected_hypotheses = _build_pairwise_hypotheses(actual)
        hypotheses = tuple(sorted(self.pairwise_hypotheses, key=lambda item: item.hypothesis_id))
        if tuple(item.digest() for item in hypotheses) != tuple(
            item.digest() for item in expected_hypotheses
        ):
            raise ValueError("pairwise hypotheses differ from the exact candidate comparison matrix")
        object.__setattr__(self, "pairwise_hypotheses", hypotheses)
        if set(self.holm_family.hypothesis_ids) != {
            item.hypothesis_id for item in hypotheses
        }:
            raise ValueError("Holm family does not exactly cover pairwise promotion claims")

    @property
    def all_main_identity_digests(self) -> tuple[Digest, ...]:
        return tuple(identity for route in self.routes for identity in route.identity_digests)

    @property
    def all_research_identity_digests(self) -> tuple[Digest, ...]:
        ablations = tuple(
            item.ablated_artifact.identity_digest()
            for item in self.ablation_registry.materialized
            if item.ablated_artifact is not None
        )
        return self.all_main_identity_digests + ablations

    @property
    def fixed_opponent_identity_digests(self) -> tuple[Digest, ...]:
        return tuple(
            identity for family in self.fixed_families for identity in family.identity_digests
        )

    @property
    def known_nonheldout_identity_digests(self) -> tuple[Digest, ...]:
        return _known_nonheldout_identities(
            self.routes,
            self.ablation_registry,
            self.fixed_families,
            self.opponent_splits,
        )

    def digest(self) -> Digest:
        """Unique pre-beacon root; contains no heldout salt or identity."""

        return _payload_digest(
            {
                "schema": "complete-formal-matrix-v2",
                "route_digests": tuple(item.digest() for item in self.routes),
                "checkpoint_freeze_digests": tuple(
                    item.digest() for item in self.checkpoint_freezes
                ),
                "ablation_registry_digest": self.ablation_registry.digest(),
                "fixed_family_digests": tuple(
                    item.manifest_digest() for item in self.fixed_families
                ),
                "opponent_split_digest": self.opponent_splits.digest(),
                "heldout_precommitment_digest": self.heldout_precommitment.digest(),
                "budget_profile_digests": tuple(item.digest() for item in self.budget_profiles),
                "shared_contracts_digest": self.contracts.digest(),
                "planned_template_digests": tuple(
                    item.digest() for item in self.planned_templates
                ),
                "pairwise_hypothesis_digests": tuple(
                    item.digest() for item in self.pairwise_hypotheses
                ),
                "holm_family_digest": self.holm_family.digest(),
            }
        )

    @property
    def freeze_root_digest(self) -> Digest:
        return self.digest()

    def assert_complete_registration(self) -> None:
        """Verify the complete deterministic matrix schema and builder origin.

        This is deliberately weaker than formal authority.  It is sufficient
        for diagnostic projection/tests, but never for a strength result.
        """

        self._validate_content()
        if self._formal_token is not _MATRIX_AUTHORITY:
            raise ValueError("matrix lacks the complete preregistration gate authority")

    def assert_formal_authority(self) -> None:
        self.assert_complete_registration()
        for receipt in self.checkpoint_freezes:
            receipt._assert_formal_authority()


def build_complete_formal_matrix(
    *,
    routes: Sequence[RouteArtifacts],
    checkpoint_freezes: Sequence[CheckpointFreezeReceipt],
    ablation_registry: MandatoryAblationRegistry,
    current_pool: Sequence[ArtifactIdentity],
    stable_anchor: ArtifactIdentity,
    nemesis: Sequence[ArtifactIdentity],
    train_opponents: Sequence[ArtifactIdentity],
    dev_opponents: Sequence[ArtifactIdentity],
    validation_opponents: Sequence[ArtifactIdentity],
    heldout_precommitment: HeldoutPrecommitment,
    resource_profiles: Mapping[int, ResourceProfile],
    contracts: MatrixSharedContracts,
) -> CompleteFormalMatrix:
    """Build the sole formal pre-beacon freeze root.

    The previous six-artifact/120-cell builder is intentionally not accepted
    here.  ``build_legacy_diagnostic_matrix`` is the explicit non-formal entry
    point for reading old plans.
    """

    routes = tuple(routes)
    families = (
        FrozenOpponentFamily("current-pool", tuple(current_pool)),
        FrozenOpponentFamily("stable-anchor", (stable_anchor,)),
        FrozenOpponentFamily("nemesis-exploit", tuple(nemesis)),
    )
    splits = OpponentSplitFreeze(
        train_identity_digests=tuple(item.identity_digest() for item in train_opponents),
        dev_identity_digests=tuple(item.identity_digest() for item in dev_opponents),
        validation_identity_digests=tuple(
            item.identity_digest() for item in validation_opponents
        ),
    )
    budgets = tuple(BudgetProfile(key, value) for key, value in resource_profiles.items())
    templates = _build_expected_templates(
        routes=routes,
        ablations=ablation_registry,
        families=families,
        heldout=heldout_precommitment,
        budget_profiles=budgets,
        contracts=contracts,
    )
    hypotheses = _build_pairwise_hypotheses(templates)
    matrix = CompleteFormalMatrix(
        routes=routes,
        checkpoint_freezes=tuple(checkpoint_freezes),
        ablation_registry=ablation_registry,
        fixed_families=families,
        opponent_splits=splits,
        heldout_precommitment=heldout_precommitment,
        budget_profiles=budgets,
        contracts=contracts,
        planned_templates=templates,
        pairwise_hypotheses=hypotheses,
        holm_family=HolmFamilyContract(
            tuple(item.hypothesis_id for item in hypotheses)
        ),
    )
    object.__setattr__(matrix, "_formal_token", _MATRIX_AUTHORITY)
    matrix.assert_complete_registration()
    return matrix


@dataclass(frozen=True, slots=True)
class HeldoutSelectionReceipt:
    complete_matrix_root_digest: Digest
    final_entropy_plan_digest: Digest
    freeze_receipt_digest: Digest
    beacon_receipt_digest: Digest
    precommitment_digest: Digest
    reveal_digest: Digest
    ordered_selected_identity_digests: tuple[Digest, Digest, Digest, Digest]
    selected_family_manifest_digest: Digest
    _selection_token: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "complete_matrix_root_digest",
            "final_entropy_plan_digest",
            "freeze_receipt_digest",
            "beacon_receipt_digest",
            "precommitment_digest",
            "reveal_digest",
            "selected_family_manifest_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        selected = tuple(
            _digest(value, "selected heldout identity")
            for value in self.ordered_selected_identity_digests
        )
        if len(selected) != HELDOUT_SELECTION_COUNT or len(set(selected)) != len(selected):
            raise ValueError("heldout selection must contain four unique identities")
        object.__setattr__(self, "ordered_selected_identity_digests", selected)

    @property
    def matrix_digest(self) -> Digest:
        return self.complete_matrix_root_digest

    def digest(self) -> Digest:
        return _payload_digest(
            {
                name: getattr(self, name)
                for name in (
                    "complete_matrix_root_digest",
                    "final_entropy_plan_digest",
                    "freeze_receipt_digest",
                    "beacon_receipt_digest",
                    "precommitment_digest",
                    "reveal_digest",
                    "ordered_selected_identity_digests",
                    "selected_family_manifest_digest",
                )
            }
        )

    def __copy__(self) -> "HeldoutSelectionReceipt":
        return type(self)(
            complete_matrix_root_digest=self.complete_matrix_root_digest,
            final_entropy_plan_digest=self.final_entropy_plan_digest,
            freeze_receipt_digest=self.freeze_receipt_digest,
            beacon_receipt_digest=self.beacon_receipt_digest,
            precommitment_digest=self.precommitment_digest,
            reveal_digest=self.reveal_digest,
            ordered_selected_identity_digests=self.ordered_selected_identity_digests,
            selected_family_manifest_digest=self.selected_family_manifest_digest,
        )

    def __deepcopy__(self, memo: dict[int, object]) -> "HeldoutSelectionReceipt":
        return self.__copy__()

    def assert_for(
        self,
        matrix: CompleteFormalMatrix,
        final_plan: FinalEvaluationPlan,
        beacon: VerifiedBeacon,
    ) -> None:
        self._assert_for(matrix, final_plan, beacon, require_formal=True)

    def assert_diagnostic_for(
        self,
        matrix: CompleteFormalMatrix,
        final_plan: FinalEvaluationPlan,
        beacon: VerifiedBeacon,
    ) -> None:
        self._assert_for(matrix, final_plan, beacon, require_formal=False)

    def _assert_for(
        self,
        matrix: CompleteFormalMatrix,
        final_plan: FinalEvaluationPlan,
        beacon: VerifiedBeacon,
        *,
        require_formal: bool,
    ) -> None:
        if require_formal:
            matrix.assert_formal_authority()
            final_plan._assert_formal()
            expected_token = _SELECTION_AUTHORITY
        else:
            matrix.assert_complete_registration()
            final_plan._assert_derivation_authority()
            expected_token = _DIAGNOSTIC_SELECTION_AUTHORITY
        if require_formal:
            beacon._assert_formal_for(final_plan)
        else:
            beacon._assert_for(final_plan)
        if self._selection_token is not expected_token:
            raise ValueError("heldout selection lacks post-freeze future-beacon authority")
        expected_family = _payload_digest(
            {
                "role": HELDOUT_ROLE,
                "ordered_member_identity_digests": (
                    self.ordered_selected_identity_digests
                ),
            }
        )
        if (
            self.complete_matrix_root_digest != matrix.digest()
            or self.final_entropy_plan_digest != _plan_digest(final_plan)
            or self.freeze_receipt_digest != final_plan.freeze_receipt_digest
            or self.beacon_receipt_digest != beacon.receipt_digest
            or self.precommitment_digest != matrix.heldout_precommitment.digest()
            or self.selected_family_manifest_digest != expected_family
        ):
            raise ValueError("heldout selection receipt differs from the frozen future-beacon inputs")


@dataclass(frozen=True, slots=True)
class MaterializedStratumReceipt:
    complete_matrix_root_digest: Digest
    planned_template_digest: Digest
    selection_receipt_digest: Digest
    resolved_focal_identity_digest: Digest
    resolved_counterparty_identity_digest: Digest
    resolved_opponent_family_manifest_digest: Digest
    evaluation_stratum_digest: Digest
    seed_cohort_digest: Digest
    bootstrap_seed: int
    sign_flip_seed: int

    def __post_init__(self) -> None:
        for name in (
            "complete_matrix_root_digest",
            "planned_template_digest",
            "selection_receipt_digest",
            "resolved_focal_identity_digest",
            "resolved_counterparty_identity_digest",
            "resolved_opponent_family_manifest_digest",
            "evaluation_stratum_digest",
            "seed_cohort_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        for name in ("bootstrap_seed", "sign_flip_seed"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value < 2**63:
                raise ValueError(f"{name} must be a future-beacon uint63")

    @property
    def matrix_digest(self) -> Digest:
        return self.complete_matrix_root_digest

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class MaterializedStratum:
    template: PlannedStratumTemplate
    stratum: EvaluationStratum
    receipt: MaterializedStratumReceipt

    def __post_init__(self) -> None:
        if self.template.digest() != self.receipt.planned_template_digest:
            raise ValueError("materialized receipt belongs to another template")
        if self.stratum.digest() != self.receipt.evaluation_stratum_digest:
            raise ValueError("materialized receipt belongs to another stratum")
        if self.template.seed_cohort_digest != self.receipt.seed_cohort_digest:
            raise ValueError("materialized receipt changed the common seed cohort")

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "template_digest": self.template.digest(),
                "stratum_digest": self.stratum.digest(),
                "receipt_digest": self.receipt.digest(),
            }
        )


@dataclass(frozen=True, slots=True)
class FormalMatrixProjection:
    complete_matrix_root_digest: Digest
    result_authority: str
    selection_receipt: HeldoutSelectionReceipt
    selected_heldout_artifacts: tuple[ArtifactIdentity, ...]
    strata: tuple[MaterializedStratum, ...]
    _projection_token: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "complete_matrix_root_digest",
            _digest(self.complete_matrix_root_digest, "projection matrix root"),
        )
        if self.result_authority not in {
            "development_diagnostic_only",
            "formal_strength",
        }:
            raise ValueError("unknown matrix projection result authority")
        selected = tuple(self.selected_heldout_artifacts)
        if self.selection_receipt.complete_matrix_root_digest != self.complete_matrix_root_digest:
            raise ValueError("projection and heldout selection use different matrix roots")
        if tuple(item.identity_digest() for item in selected) != (
            self.selection_receipt.ordered_selected_identity_digests
        ):
            raise ValueError("projection heldout artifacts differ from the selection receipt")
        if len({item.template.digest() for item in self.strata}) != len(self.strata):
            raise ValueError("projection contains duplicate materialized templates")

    def __copy__(self) -> "FormalMatrixProjection":
        return type(self)(
            complete_matrix_root_digest=self.complete_matrix_root_digest,
            result_authority=self.result_authority,
            selection_receipt=copy.copy(self.selection_receipt),
            selected_heldout_artifacts=self.selected_heldout_artifacts,
            strata=self.strata,
        )

    def __deepcopy__(self, memo: dict[int, object]) -> "FormalMatrixProjection":
        return type(self)(
            complete_matrix_root_digest=self.complete_matrix_root_digest,
            result_authority=self.result_authority,
            selection_receipt=copy.deepcopy(self.selection_receipt, memo),
            selected_heldout_artifacts=copy.deepcopy(
                self.selected_heldout_artifacts, memo
            ),
            strata=copy.deepcopy(self.strata, memo),
        )

    @property
    def matrix_digest(self) -> Digest:
        return self.complete_matrix_root_digest

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "schema": "formal-matrix-post-beacon-projection-v2",
                "complete_matrix_root_digest": self.complete_matrix_root_digest,
                "result_authority": self.result_authority,
                "selection_receipt_digest": self.selection_receipt.digest(),
                "selected_heldout_identity_digests": tuple(
                    item.identity_digest() for item in self.selected_heldout_artifacts
                ),
                "materialized_stratum_digests": tuple(item.digest() for item in self.strata),
            }
        )

    def assert_for(
        self,
        matrix: CompleteFormalMatrix,
        final_plan: FinalEvaluationPlan,
        beacon: VerifiedBeacon,
    ) -> None:
        self._assert_for(matrix, final_plan, beacon, require_formal=True)

    def assert_diagnostic_for(
        self,
        matrix: CompleteFormalMatrix,
        final_plan: FinalEvaluationPlan,
        beacon: VerifiedBeacon,
    ) -> None:
        self._assert_for(matrix, final_plan, beacon, require_formal=False)

    def _assert_for(
        self,
        matrix: CompleteFormalMatrix,
        final_plan: FinalEvaluationPlan,
        beacon: VerifiedBeacon,
        *,
        require_formal: bool,
    ) -> None:
        if require_formal:
            matrix.assert_formal_authority()
            final_plan._assert_formal()
            expected_authority = "formal_strength"
            expected_token = _PROJECTION_AUTHORITY
        else:
            matrix.assert_complete_registration()
            final_plan._assert_derivation_authority()
            expected_authority = "development_diagnostic_only"
            expected_token = _DIAGNOSTIC_PROJECTION_AUTHORITY
        if require_formal:
            beacon._assert_formal_for(final_plan)
        else:
            beacon._assert_for(final_plan)
        if self.result_authority != expected_authority:
            raise ValueError("matrix projection authority differs from the requested path")
        if self._projection_token is not expected_token:
            raise ValueError("projection lacks post-beacon materialization authority")
        if self.complete_matrix_root_digest != matrix.digest():
            raise ValueError("projection belongs to another complete matrix")
        if final_plan.complete_formal_matrix_root_digest != matrix.digest():
            raise ValueError("future entropy plan belongs to another complete matrix")
        if require_formal:
            self.selection_receipt.assert_for(matrix, final_plan, beacon)
        else:
            self.selection_receipt.assert_diagnostic_for(matrix, final_plan, beacon)
        if tuple(
            item.identity_digest() for item in self.selected_heldout_artifacts
        ) != self.selection_receipt.ordered_selected_identity_digests:
            raise ValueError("projection selected artifacts changed after materialization")
        if len(self.strata) != len(matrix.planned_templates):
            raise ValueError("projection does not cover the complete matrix")
        if tuple(item.template.digest() for item in self.strata) != tuple(
            item.digest() for item in matrix.planned_templates
        ):
            raise ValueError("projection template order/content differs from the freeze root")
        for item in self.strata:
            template = item.template
            if template.key.kind == "heldout-slot":
                assert template.key.heldout_slot is not None
                slot_index = HELDOUT_SLOT_IDS.index(template.key.heldout_slot)
                counterparty = self.selection_receipt.ordered_selected_identity_digests[
                    slot_index
                ]
                family = self.selection_receipt.selected_family_manifest_digest
            else:
                assert template.counterparty_identity_digest is not None
                counterparty = template.counterparty_identity_digest
                family = template.opponent_family_template_digest
            expected_stratum = EvaluationStratum(
                identity_pair=tuple(
                    sorted((template.focal_identity_digest, counterparty))
                ),  # type: ignore[arg-type]
                split=template.split,
                opponent_family_manifest_digest=family,
                rules_digest=matrix.contracts.rules_digest,
                harness_digest=matrix.contracts.harness_digest,
                deal_generator_digest=matrix.contracts.deal_generator_digest,
                resource_profile_digest=template.resource_profile_digest,
                time_budget_ms=template.key.budget_ms,
                comparison_mode=template.key.comparison_mode,
                hypothesis_digest=template.hypothesis_id,
                multiplicity_family_digest=matrix.holm_family.digest(),
            )
            expected_receipt = MaterializedStratumReceipt(
                complete_matrix_root_digest=matrix.digest(),
                planned_template_digest=template.digest(),
                selection_receipt_digest=self.selection_receipt.digest(),
                resolved_focal_identity_digest=template.focal_identity_digest,
                resolved_counterparty_identity_digest=counterparty,
                resolved_opponent_family_manifest_digest=family,
                evaluation_stratum_digest=expected_stratum.digest(),
                seed_cohort_digest=template.seed_cohort_digest,
                bootstrap_seed=final_plan.derive_formal_analysis_seed(
                    beacon,
                    seed_cohort_digest=template.seed_cohort_digest,
                    hypothesis_digest=template.hypothesis_id,
                    analysis_domain="bootstrap",
                ),
                sign_flip_seed=final_plan.derive_formal_analysis_seed(
                    beacon,
                    seed_cohort_digest=template.seed_cohort_digest,
                    hypothesis_digest=template.hypothesis_id,
                    analysis_domain="sign-flip",
                ),
            )
            if (
                item.stratum.digest() != expected_stratum.digest()
                or item.receipt.digest() != expected_receipt.digest()
            ):
                raise ValueError("projection stratum differs from deterministic materialization")


# Old name retained only as a type alias for readers; this object is a
# post-beacon projection, never a second formal root.
MaterializedFormalMatrix = FormalMatrixProjection


def _plan_digest(plan: FinalEvaluationPlan) -> Digest:
    return _payload_digest(plan.to_dict())


def materialize_formal_matrix(
    matrix: CompleteFormalMatrix,
    final_plan: FinalEvaluationPlan,
    beacon: VerifiedBeacon,
    heldout_reveal: HeldoutReveal,
) -> FormalMatrixProjection:
    """Formal projection; currently fails closed at the entropy trust boundary."""

    matrix.assert_formal_authority()
    final_plan._assert_formal()
    return _materialize_matrix_projection(
        matrix,
        final_plan,
        beacon,
        heldout_reveal,
        result_authority="formal_strength",
    )


def materialize_diagnostic_matrix_projection(
    matrix: CompleteFormalMatrix,
    final_plan: FinalEvaluationPlan,
    beacon: VerifiedBeacon,
    heldout_reveal: HeldoutReveal,
) -> FormalMatrixProjection:
    """Exercise deterministic projection without granting strength authority."""

    matrix.assert_complete_registration()
    final_plan._assert_derivation_authority()
    return _materialize_matrix_projection(
        matrix,
        final_plan,
        beacon,
        heldout_reveal,
        result_authority="development_diagnostic_only",
    )


def _materialize_matrix_projection(
    matrix: CompleteFormalMatrix,
    final_plan: FinalEvaluationPlan,
    beacon: VerifiedBeacon,
    heldout_reveal: HeldoutReveal,
    *,
    result_authority: str,
) -> FormalMatrixProjection:
    if result_authority == "formal_strength":
        beacon._assert_formal_for(final_plan)
    else:
        beacon._assert_for(final_plan)
    if final_plan.complete_formal_matrix_root_digest != matrix.digest():
        raise ValueError("future entropy plan belongs to a different complete formal matrix")
    heldout_reveal.verify(
        matrix.heldout_precommitment,
        matrix.known_nonheldout_identity_digests,
    )
    selected_identities = final_plan.rank_opponents(
        beacon, heldout_reveal.identity_digests
    )[:HELDOUT_SELECTION_COUNT]
    artifact_by_identity = {
        item.identity_digest(): item for item in heldout_reveal.universe
    }
    selected_artifacts = tuple(artifact_by_identity[value] for value in selected_identities)
    # Heldout is not a fixed family role, so bind the ordered selected content
    # directly rather than abusing FrozenOpponentFamily's enum.
    selected_family_manifest = _payload_digest(
        {"role": HELDOUT_ROLE, "ordered_member_identity_digests": selected_identities}
    )
    selection = HeldoutSelectionReceipt(
        complete_matrix_root_digest=matrix.digest(),
        final_entropy_plan_digest=_plan_digest(final_plan),
        freeze_receipt_digest=final_plan.freeze_receipt_digest,
        beacon_receipt_digest=beacon.receipt_digest,
        precommitment_digest=matrix.heldout_precommitment.digest(),
        reveal_digest=heldout_reveal.digest(),
        ordered_selected_identity_digests=selected_identities,  # type: ignore[arg-type]
        selected_family_manifest_digest=selected_family_manifest,
    )
    selection_token = (
        _SELECTION_AUTHORITY
        if result_authority == "formal_strength"
        else _DIAGNOSTIC_SELECTION_AUTHORITY
    )
    object.__setattr__(selection, "_selection_token", selection_token)

    materialized: list[MaterializedStratum] = []
    for template in matrix.planned_templates:
        if template.key.kind == "heldout-slot":
            assert template.key.heldout_slot is not None
            index = HELDOUT_SLOT_IDS.index(template.key.heldout_slot)
            counterparty = selected_identities[index]
            opponent_family = selected_family_manifest
        else:
            assert template.counterparty_identity_digest is not None
            counterparty = template.counterparty_identity_digest
            opponent_family = template.opponent_family_template_digest
        identity_pair = tuple(sorted((template.focal_identity_digest, counterparty)))
        stratum = EvaluationStratum(
            identity_pair=identity_pair,  # type: ignore[arg-type]
            split=template.split,
            opponent_family_manifest_digest=opponent_family,
            rules_digest=matrix.contracts.rules_digest,
            harness_digest=matrix.contracts.harness_digest,
            deal_generator_digest=matrix.contracts.deal_generator_digest,
            resource_profile_digest=template.resource_profile_digest,
            time_budget_ms=template.key.budget_ms,
            comparison_mode=template.key.comparison_mode,
            hypothesis_digest=template.hypothesis_id,
            multiplicity_family_digest=matrix.holm_family.digest(),
        )
        receipt = MaterializedStratumReceipt(
            complete_matrix_root_digest=matrix.digest(),
            planned_template_digest=template.digest(),
            selection_receipt_digest=selection.digest(),
            resolved_focal_identity_digest=template.focal_identity_digest,
            resolved_counterparty_identity_digest=counterparty,
            resolved_opponent_family_manifest_digest=opponent_family,
            evaluation_stratum_digest=stratum.digest(),
            seed_cohort_digest=template.seed_cohort_digest,
            bootstrap_seed=final_plan.derive_formal_analysis_seed(
                beacon,
                seed_cohort_digest=template.seed_cohort_digest,
                hypothesis_digest=template.hypothesis_id,
                analysis_domain="bootstrap",
            ),
            sign_flip_seed=final_plan.derive_formal_analysis_seed(
                beacon,
                seed_cohort_digest=template.seed_cohort_digest,
                hypothesis_digest=template.hypothesis_id,
                analysis_domain="sign-flip",
            ),
        )
        materialized.append(MaterializedStratum(template, stratum, receipt))
    projection = FormalMatrixProjection(
        complete_matrix_root_digest=matrix.digest(),
        result_authority=result_authority,
        selection_receipt=selection,
        selected_heldout_artifacts=selected_artifacts,
        strata=tuple(materialized),
    )
    projection_token = (
        _PROJECTION_AUTHORITY
        if result_authority == "formal_strength"
        else _DIAGNOSTIC_PROJECTION_AUTHORITY
    )
    object.__setattr__(projection, "_projection_token", projection_token)
    if result_authority == "formal_strength":
        projection.assert_for(matrix, final_plan, beacon)
    else:
        projection.assert_diagnostic_for(matrix, final_plan, beacon)
    return projection


def _evaluation_bundle_projection_digest(
    matrix: CompleteFormalMatrix,
    projection: FormalMatrixProjection,
) -> Digest:
    """Bind the complete matrix as a post-beacon evaluation bundle.

    This is deliberately *not* the legacy ``CandidateBundleManifest`` digest.
    That older schema can name exactly three identities and therefore cannot
    represent the twelve checkpoint/mode artifacts plus ablations required by
    this matrix.  The future-entropy root remains ``matrix.digest()``; this
    projection only gives the evaluation layer one compact typed bundle link.
    """

    return _payload_digest(
        {
            "schema": "complete-formal-matrix-evaluation-bundle-projection-v1",
            "complete_matrix_root_digest": matrix.digest(),
            "projection_digest": projection.digest(),
            "research_identity_digests": tuple(
                sorted(matrix.all_research_identity_digests)
            ),
            "known_nonheldout_identity_digests": tuple(
                sorted(matrix.known_nonheldout_identity_digests)
            ),
            "selected_heldout_identity_digests": (
                projection.selection_receipt.ordered_selected_identity_digests
            ),
            "opponent_split_digest": matrix.opponent_splits.digest(),
            "resource_profile_digests": tuple(
                item.profile.digest() for item in matrix.budget_profiles
            ),
            "shared_contracts_digest": matrix.contracts.digest(),
            "materialized_stratum_digests": tuple(
                item.digest() for item in projection.strata
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class FormalEvaluationPlanBridge:
    """Sealed matrix-to-evaluation input for exactly one materialized cell.

    The bridge removes two caller-controlled gaps: a caller cannot substitute
    the legacy exactly-three-candidate bundle, and cannot choose a stratum
    digest as a replacement for the candidate-neutral seed cohort.  Formal
    evaluation must consume this exact issued instance and revalidate it
    against the matrix/projection/future-entropy objects.
    """

    result_authority: str
    complete_matrix_root_digest: Digest
    projection_digest: Digest
    planned_template_digest: Digest
    evaluation_stratum_digest: Digest
    seed_cohort_digest: Digest
    candidate_bundle_digest: Digest
    ordered_artifact_identity_digests: tuple[Digest, Digest]
    resource_profile_digest: Digest
    paired_block_count: int
    bootstrap_seed: int
    sign_flip_seed: int
    stopping_rule_digest: Digest
    retry_policy_digest: Digest
    analysis_code_digest: Digest
    replay_verifier_digest: Digest
    oracle_fixture_digest: Digest
    infrastructure_monitor_digest: Digest
    freeze_receipt_digest: Digest
    beacon_receipt_digest: Digest
    _authority_guard: object = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.result_authority not in {
            "development_diagnostic_only",
            "formal_strength",
        }:
            raise ValueError("unknown evaluation-plan bridge authority")
        for name in (
            "complete_matrix_root_digest",
            "projection_digest",
            "planned_template_digest",
            "evaluation_stratum_digest",
            "seed_cohort_digest",
            "candidate_bundle_digest",
            "resource_profile_digest",
            "stopping_rule_digest",
            "retry_policy_digest",
            "analysis_code_digest",
            "replay_verifier_digest",
            "oracle_fixture_digest",
            "infrastructure_monitor_digest",
            "freeze_receipt_digest",
            "beacon_receipt_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        identities = tuple(
            _digest(value, "bridge artifact identity")
            for value in self.ordered_artifact_identity_digests
        )
        if len(identities) != 2 or identities[0] == identities[1]:
            raise ValueError("evaluation-plan bridge requires two ordered identities")
        object.__setattr__(self, "ordered_artifact_identity_digests", identities)
        if type(self.paired_block_count) is not int or self.paired_block_count <= 0:
            raise ValueError("evaluation-plan bridge block count must be positive")
        for name in ("bootstrap_seed", "sign_flip_seed"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value < 2**63:
                raise ValueError(f"{name} must be a future-entropy uint63")
        if self.bootstrap_seed == self.sign_flip_seed:
            raise ValueError("bootstrap and sign-flip streams must be domain-separated")

    def _unchecked_digest(self) -> Digest:
        payload = asdict(self)
        payload.pop("_authority_guard", None)
        return _payload_digest(payload)

    def _assert_issued(self) -> None:
        guard = self._authority_guard
        if not callable(guard) or guard(self) is not True:
            raise ValueError(
                "evaluation-plan bridge was copied, forged, altered, or not matrix-issued"
            )

    def digest(self) -> Digest:
        self._assert_issued()
        return self._unchecked_digest()

    def sealed_payload(self) -> dict[str, object]:
        """Return exact authority-owned inputs for FormalEvaluationPlan."""

        self._assert_issued()
        payload = asdict(self)
        payload.pop("_authority_guard", None)
        payload["bridge_digest"] = self._unchecked_digest()
        return payload

    def assert_for(
        self,
        matrix: CompleteFormalMatrix,
        projection: FormalMatrixProjection,
        materialized: MaterializedStratum,
        final_plan: FinalEvaluationPlan,
        beacon: VerifiedBeacon,
    ) -> None:
        self._assert_for(
            matrix,
            projection,
            materialized,
            final_plan,
            beacon,
            require_formal=True,
        )

    def assert_diagnostic_for(
        self,
        matrix: CompleteFormalMatrix,
        projection: FormalMatrixProjection,
        materialized: MaterializedStratum,
        final_plan: FinalEvaluationPlan,
        beacon: VerifiedBeacon,
    ) -> None:
        self._assert_for(
            matrix,
            projection,
            materialized,
            final_plan,
            beacon,
            require_formal=False,
        )

    def _assert_for(
        self,
        matrix: CompleteFormalMatrix,
        projection: FormalMatrixProjection,
        materialized: MaterializedStratum,
        final_plan: FinalEvaluationPlan,
        beacon: VerifiedBeacon,
        *,
        require_formal: bool,
    ) -> None:
        self._assert_issued()
        if require_formal:
            matrix.assert_formal_authority()
            projection.assert_for(matrix, final_plan, beacon)
            expected_authority = "formal_strength"
        else:
            matrix.assert_complete_registration()
            projection.assert_diagnostic_for(matrix, final_plan, beacon)
            expected_authority = "development_diagnostic_only"
        if self.result_authority != expected_authority:
            raise ValueError("evaluation-plan bridge authority differs from requested path")
        matches = [
            item
            for item in projection.strata
            if item.template.digest() == materialized.template.digest()
        ]
        if len(matches) != 1 or matches[0].digest() != materialized.digest():
            raise ValueError("evaluation-plan bridge materialized cell is not in projection")
        expected = _evaluation_plan_bridge_fields(
            matrix,
            projection,
            materialized,
            final_plan,
            beacon,
            result_authority=expected_authority,
        )
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"evaluation-plan bridge field changed: {name}")


def _evaluation_plan_bridge_fields(
    matrix: CompleteFormalMatrix,
    projection: FormalMatrixProjection,
    materialized: MaterializedStratum,
    final_plan: FinalEvaluationPlan,
    beacon: VerifiedBeacon,
    *,
    result_authority: str,
) -> dict[str, object]:
    if final_plan.complete_formal_matrix_root_digest != matrix.digest():
        raise ValueError("evaluation-plan bridge entropy root differs from matrix")
    beacon._assert_for(final_plan)
    receipt = materialized.receipt
    if receipt.complete_matrix_root_digest != matrix.digest():
        raise ValueError("materialized cell belongs to another matrix")
    profiles = [
        item.profile
        for item in matrix.budget_profiles
        if item.profile.digest() == materialized.stratum.resource_profile_digest
    ]
    if len(profiles) != 1:
        raise ValueError("materialized cell does not resolve one frozen resource profile")
    return {
        "result_authority": result_authority,
        "complete_matrix_root_digest": matrix.digest(),
        "projection_digest": projection.digest(),
        "planned_template_digest": materialized.template.digest(),
        "evaluation_stratum_digest": materialized.stratum.digest(),
        "seed_cohort_digest": materialized.template.seed_cohort_digest,
        "candidate_bundle_digest": _evaluation_bundle_projection_digest(
            matrix, projection
        ),
        "ordered_artifact_identity_digests": (
            receipt.resolved_focal_identity_digest,
            receipt.resolved_counterparty_identity_digest,
        ),
        "resource_profile_digest": profiles[0].digest(),
        "paired_block_count": materialized.template.paired_block_count,
        "bootstrap_seed": receipt.bootstrap_seed,
        "sign_flip_seed": receipt.sign_flip_seed,
        "stopping_rule_digest": matrix.contracts.stopping_rule_digest,
        "retry_policy_digest": matrix.contracts.retry_policy_digest,
        "analysis_code_digest": matrix.contracts.analysis_code_digest,
        "replay_verifier_digest": matrix.contracts.replay_verifier_digest,
        "oracle_fixture_digest": matrix.contracts.oracle_fixture_digest,
        "infrastructure_monitor_digest": (
            matrix.contracts.infrastructure_monitor_digest
        ),
        "freeze_receipt_digest": final_plan.freeze_receipt_digest,
        "beacon_receipt_digest": beacon.receipt_digest,
    }


def _build_evaluation_plan_bridge(
    matrix: CompleteFormalMatrix,
    projection: FormalMatrixProjection,
    materialized: MaterializedStratum,
    final_plan: FinalEvaluationPlan,
    beacon: VerifiedBeacon,
    *,
    result_authority: str,
) -> FormalEvaluationPlanBridge:
    fields = _evaluation_plan_bridge_fields(
        matrix,
        projection,
        materialized,
        final_plan,
        beacon,
        result_authority=result_authority,
    )
    bridge = FormalEvaluationPlanBridge(**fields)  # type: ignore[arg-type]
    sealed = bridge._unchecked_digest()

    def issued_instance(
        candidate: object,
        owner: object = bridge,
        content_digest: Digest = sealed,
    ) -> bool:
        return (
            candidate is owner
            and isinstance(candidate, FormalEvaluationPlanBridge)
            and candidate._unchecked_digest() == content_digest
        )

    object.__setattr__(bridge, "_authority_guard", issued_instance)
    return bridge


def build_formal_evaluation_plan_bridge(
    matrix: CompleteFormalMatrix,
    projection: FormalMatrixProjection,
    materialized: MaterializedStratum,
    final_plan: FinalEvaluationPlan,
    beacon: VerifiedBeacon,
) -> FormalEvaluationPlanBridge:
    """Issue one formal bridge; unavailable authorities fail before issuance."""

    matrix.assert_formal_authority()
    projection.assert_for(matrix, final_plan, beacon)
    bridge = _build_evaluation_plan_bridge(
        matrix,
        projection,
        materialized,
        final_plan,
        beacon,
        result_authority="formal_strength",
    )
    bridge.assert_for(matrix, projection, materialized, final_plan, beacon)
    return bridge


def build_diagnostic_evaluation_plan_bridge(
    matrix: CompleteFormalMatrix,
    projection: FormalMatrixProjection,
    materialized: MaterializedStratum,
    final_plan: FinalEvaluationPlan,
    beacon: VerifiedBeacon,
) -> FormalEvaluationPlanBridge:
    """Exercise the exact bridge mapping without granting strength authority."""

    matrix.assert_complete_registration()
    projection.assert_diagnostic_for(matrix, final_plan, beacon)
    bridge = _build_evaluation_plan_bridge(
        matrix,
        projection,
        materialized,
        final_plan,
        beacon,
        result_authority="development_diagnostic_only",
    )
    bridge.assert_diagnostic_for(
        matrix, projection, materialized, final_plan, beacon
    )
    return bridge


@dataclass(frozen=True, slots=True)
class SupervisorObservationBinding:
    """One typed observation mapped to one signed terminal attempt row."""

    observation_digest: Digest
    leg_plan_digest: Digest
    supervisor_readiness_attestation_digest: Digest
    supervisor_attempt_journal_scope_digest: Digest
    supervisor_attempt_sequence: int
    supervisor_previous_attempt_entry_digest: Digest
    supervisor_leg_run_id: Digest
    supervisor_launch_authorization_digest: Digest
    supervisor_leg_receipt_digest: Digest
    supervisor_receipt_consumption_key: Digest
    supervisor_consumption_ledger_entry_digest: Digest
    supervisor_consumption_ledger_entry_inode: int
    supervisor_consumption_ledger_entry_path: str
    supervisor_capture_session_digest: Digest
    supervisor_cleanup_receipt_digest: Digest
    raw_wire_digest: Digest
    supervisor_wire_semantic_digest: Digest
    supervisor_replay_digest: Digest
    replay_verification_digest: Digest

    def __post_init__(self) -> None:
        digest_fields = tuple(
            name
            for name in self.__dataclass_fields__
            if name
            not in {
                "supervisor_attempt_sequence",
                "supervisor_consumption_ledger_entry_inode",
                "supervisor_consumption_ledger_entry_path",
            }
        )
        for name in digest_fields:
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if (
            type(self.supervisor_attempt_sequence) is not int
            or self.supervisor_attempt_sequence < 1
        ):
            raise ValueError("supervisor attempt sequence must be positive")
        if (
            type(self.supervisor_consumption_ledger_entry_inode) is not int
            or self.supervisor_consumption_ledger_entry_inode <= 0
        ):
            raise ValueError("supervisor consumption ledger inode must be positive")
        path = self.supervisor_consumption_ledger_entry_path
        if not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/"):
            raise ValueError("supervisor consumption ledger path must be absolute")

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))

    def matches_attempt_entry(self, entry: object) -> bool:
        expected = {
            "leg_plan_digest": self.leg_plan_digest,
            "readiness_attestation_digest": (
                self.supervisor_readiness_attestation_digest
            ),
            "attempt_journal_scope_digest": (
                self.supervisor_attempt_journal_scope_digest
            ),
            "attempt_sequence": self.supervisor_attempt_sequence,
            "previous_attempt_entry_digest": (
                self.supervisor_previous_attempt_entry_digest
            ),
            "leg_run_id": self.supervisor_leg_run_id,
            "launch_authorization_digest": (
                self.supervisor_launch_authorization_digest
            ),
            "supervisor_leg_receipt_digest": self.supervisor_leg_receipt_digest,
            "receipt_consumption_key": self.supervisor_receipt_consumption_key,
            "capture_session_digest": self.supervisor_capture_session_digest,
            "cleanup_receipt_digest": self.supervisor_cleanup_receipt_digest,
            "raw_wire_digest": self.raw_wire_digest,
            "wire_semantic_digest": self.supervisor_wire_semantic_digest,
            "replay_digest": self.supervisor_replay_digest,
            "replay_verification_digest": self.replay_verification_digest,
        }
        return all(getattr(entry, name, None) == value for name, value in expected.items())


@dataclass(frozen=True, slots=True)
class FormalCellResult:
    projection_digest: Digest
    planned_template_digest: Digest
    evaluation_stratum_digest: Digest
    seed_cohort_digest: Digest
    focal_identity_digest: Digest
    counterparty_identity_digest: Digest
    formal_plan_digest: Digest
    aggregate_result_digest: Digest
    deck_sequence_commitment_digests: tuple[Digest, ...]
    block_plan_digests: tuple[Digest, ...]
    paired_evidence_receipt_digests: tuple[Digest, ...]
    observation_digests: tuple[Digest, ...]
    execution_receipt_digests: tuple[Digest, ...]
    resource_receipt_digests: tuple[Digest, ...]
    raw_evidence_digests: tuple[Digest, ...]
    supervisor_contract_digest: Digest
    supervisor_launch_authorization_digests: tuple[Digest, ...]
    supervisor_leg_receipt_digests: tuple[Digest, ...]
    supervisor_receipt_consumption_keys: tuple[Digest, ...]
    supervisor_control_session_digests: tuple[Digest, ...]
    supervisor_capture_session_digests: tuple[Digest, ...]
    supervisor_socket_identity_digests: tuple[Digest, ...]
    supervisor_wire_semantic_digests: tuple[Digest, ...]
    supervisor_replay_digests: tuple[Digest, ...]
    supervisor_decision_trace_digests: tuple[Digest, ...]
    supervisor_cleanup_receipt_digests: tuple[Digest, ...]
    supervisor_observation_bindings: tuple[SupervisorObservationBinding, ...]
    supervisor_retry_observation_pairs: tuple[tuple[Digest, Digest], ...]
    run_ids: tuple[Digest, ...]
    process_tree_ids: tuple[str, ...]
    cgroup_paths: tuple[str, ...]
    cgroup_inodes: tuple[int, ...]
    focal_score_by_paired_block: tuple[float, ...]
    _verified_token: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "projection_digest",
            "planned_template_digest",
            "evaluation_stratum_digest",
            "seed_cohort_digest",
            "focal_identity_digest",
            "counterparty_identity_digest",
            "formal_plan_digest",
            "aggregate_result_digest",
            "supervisor_contract_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        decks = tuple(
            _digest(value, "deck-sequence commitment")
            for value in self.deck_sequence_commitment_digests
        )
        blocks = tuple(_digest(value, "block plan digest") for value in self.block_plan_digests)
        evidence = tuple(
            _digest(value, "paired evidence receipt")
            for value in self.paired_evidence_receipt_digests
        )
        scores = tuple(self.focal_score_by_paired_block)
        evidence_vectors = {
            "observation_digests": self.observation_digests,
            "execution_receipt_digests": self.execution_receipt_digests,
            "resource_receipt_digests": self.resource_receipt_digests,
            "raw_evidence_digests": self.raw_evidence_digests,
            "supervisor_launch_authorization_digests": (
                self.supervisor_launch_authorization_digests
            ),
            "supervisor_leg_receipt_digests": self.supervisor_leg_receipt_digests,
            "supervisor_receipt_consumption_keys": (
                self.supervisor_receipt_consumption_keys
            ),
            "supervisor_capture_session_digests": self.supervisor_capture_session_digests,
            "supervisor_socket_identity_digests": self.supervisor_socket_identity_digests,
            "supervisor_wire_semantic_digests": self.supervisor_wire_semantic_digests,
            "supervisor_replay_digests": self.supervisor_replay_digests,
            "supervisor_decision_trace_digests": self.supervisor_decision_trace_digests,
            "supervisor_cleanup_receipt_digests": (
                self.supervisor_cleanup_receipt_digests
            ),
            "run_ids": self.run_ids,
        }
        for name, values in evidence_vectors.items():
            normalized = tuple(_digest(value, name) for value in values)
            if not normalized or len(set(normalized)) != len(normalized):
                raise ValueError(f"{name} must be non-empty and globally unique within a cell")
            object.__setattr__(self, name, normalized)
        control_sessions = tuple(
            _digest(value, "supervisor control session")
            for value in self.supervisor_control_session_digests
        )
        if not control_sessions:
            raise ValueError("supervisor control sessions must be non-empty")
        object.__setattr__(
            self, "supervisor_control_session_digests", control_sessions
        )
        process_trees = tuple(self.process_tree_ids)
        cgroup_paths = tuple(self.cgroup_paths)
        cgroup_inodes = tuple(self.cgroup_inodes)
        if (
            not process_trees
            or len(set(process_trees)) != len(process_trees)
            or any(not isinstance(value, str) or not value for value in process_trees)
        ):
            raise ValueError("process-tree IDs must be non-empty and unique within a cell")
        if (
            not cgroup_paths
            or len(set(cgroup_paths)) != len(cgroup_paths)
            or any(not isinstance(value, str) or not value for value in cgroup_paths)
        ):
            raise ValueError("cgroup paths must be non-empty and unique within a cell")
        if (
            not cgroup_inodes
            or len(set(cgroup_inodes)) != len(cgroup_inodes)
            or any(type(value) is not int or value <= 0 for value in cgroup_inodes)
        ):
            raise ValueError("cgroup inodes must be positive and unique within a cell")
        object.__setattr__(self, "process_tree_ids", process_trees)
        object.__setattr__(self, "cgroup_paths", cgroup_paths)
        object.__setattr__(self, "cgroup_inodes", cgroup_inodes)
        observation_count = len(self.observation_digests)
        if any(
            len(values) != observation_count
            for values in (
                self.supervisor_leg_receipt_digests,
                self.supervisor_launch_authorization_digests,
                self.supervisor_receipt_consumption_keys,
                self.supervisor_control_session_digests,
                self.supervisor_capture_session_digests,
                self.supervisor_wire_semantic_digests,
                self.supervisor_replay_digests,
                self.supervisor_decision_trace_digests,
                self.supervisor_cleanup_receipt_digests,
            )
        ):
            raise ValueError(
                "every MatchObservation requires exactly one signed supervisor leg receipt"
            )
        if len(self.supervisor_socket_identity_digests) != 2 * observation_count:
            raise ValueError(
                "every MatchObservation requires two isolated signed socket identities"
            )
        bindings = tuple(
            sorted(
                self.supervisor_observation_bindings,
                key=lambda item: item.observation_digest,
            )
        )
        if (
            len(bindings) != observation_count
            or any(not isinstance(item, SupervisorObservationBinding) for item in bindings)
            or tuple(item.observation_digest for item in bindings)
            != self.observation_digests
        ):
            raise ValueError(
                "supervisor observation bindings must exactly cover every observation"
            )
        binding_vectors = {
            "supervisor_launch_authorization_digests": tuple(
                sorted(item.supervisor_launch_authorization_digest for item in bindings)
            ),
            "supervisor_leg_receipt_digests": tuple(
                sorted(item.supervisor_leg_receipt_digest for item in bindings)
            ),
            "supervisor_receipt_consumption_keys": tuple(
                sorted(item.supervisor_receipt_consumption_key for item in bindings)
            ),
            "supervisor_capture_session_digests": tuple(
                sorted(item.supervisor_capture_session_digest for item in bindings)
            ),
            "supervisor_wire_semantic_digests": tuple(
                sorted(item.supervisor_wire_semantic_digest for item in bindings)
            ),
            "supervisor_replay_digests": tuple(
                sorted(item.supervisor_replay_digest for item in bindings)
            ),
            "supervisor_cleanup_receipt_digests": tuple(
                sorted(item.supervisor_cleanup_receipt_digest for item in bindings)
            ),
        }
        for name, expected in binding_vectors.items():
            if getattr(self, name) != expected:
                raise ValueError(
                    f"{name} differs from typed supervisor observation bindings"
                )
        object.__setattr__(self, "supervisor_observation_bindings", bindings)
        retry_pairs = tuple(
            sorted(
                (
                    _digest(original, "retry original observation"),
                    _digest(retry, "retry observation"),
                )
                for original, retry in self.supervisor_retry_observation_pairs
            )
        )
        if (
            len(set(retry_pairs)) != len(retry_pairs)
            or len({original for original, _retry in retry_pairs})
            != len(retry_pairs)
            or any(original == retry for original, retry in retry_pairs)
            or any(
                original not in self.observation_digests
                or retry not in self.observation_digests
                for original, retry in retry_pairs
            )
        ):
            raise ValueError(
                "supervisor retry pairs must be unique directed observation edges"
            )
        object.__setattr__(
            self, "supervisor_retry_observation_pairs", retry_pairs
        )
        if any(
            len(values) != 2 * observation_count
            for values in (
                self.execution_receipt_digests,
                self.resource_receipt_digests,
                self.run_ids,
                self.process_tree_ids,
                self.cgroup_paths,
                self.cgroup_inodes,
            )
        ):
            raise ValueError(
                "every MatchObservation requires exactly two execution/resource identities"
            )
        if (
            not blocks
            or len(decks) != len(blocks)
            or len(blocks) != len(evidence)
            or len(blocks) != len(scores)
        ):
            raise ValueError(
                "cell result deck/block/evidence/score vectors must align and be non-empty"
            )
        if (
            len(set(decks)) != len(decks)
            or len(set(blocks)) != len(blocks)
            or len(set(evidence)) != len(evidence)
        ):
            raise ValueError("cell result may not reuse deck, block, or evidence receipts")
        allowed = {0.0, 0.25, 0.5, 0.75, 1.0}
        if any(type(value) not in {int, float} or float(value) not in allowed for value in scores):
            raise ValueError("paired-block score must be the mean of two W/D/L match scores")
        object.__setattr__(self, "deck_sequence_commitment_digests", decks)
        object.__setattr__(self, "block_plan_digests", blocks)
        object.__setattr__(self, "paired_evidence_receipt_digests", evidence)
        object.__setattr__(self, "focal_score_by_paired_block", tuple(float(v) for v in scores))

    @classmethod
    def from_verified_paired_blocks(
        cls,
        projection: FormalMatrixProjection,
        materialized: MaterializedStratum,
        *,
        matrix: CompleteFormalMatrix,
        final_entropy_plan: FinalEvaluationPlan,
        verified_beacon: VerifiedBeacon,
        plan_bridge: FormalEvaluationPlanBridge,
        plan: FormalEvaluationPlan,
        paired_blocks: Sequence[PairedBlock],
        retry_ledger: RetryLedger = RetryLedger(),
    ) -> "FormalCellResult":
        if projection._projection_token is not _PROJECTION_AUTHORITY:
            raise ValueError("formal cell result requires an authorized matrix projection")
        if not isinstance(plan_bridge, FormalEvaluationPlanBridge):
            raise ValueError("formal cell result requires a typed matrix plan bridge")
        plan_bridge.assert_for(
            matrix,
            projection,
            materialized,
            final_entropy_plan,
            verified_beacon,
        )
        if plan.result_authority != "formal_strength":
            raise ValueError("formal cell result rejects diagnostic evaluation plans")
        plan._assert_formal_authority()
        exact_plan_bindings = {
            "complete_matrix_root_digest": matrix.digest(),
            "matrix_projection_digest": projection.digest(),
            "matrix_template_digest": materialized.template.digest(),
            "formal_seed_cohort_digest": materialized.template.seed_cohort_digest,
            "matrix_candidate_bundle_digest": plan_bridge.candidate_bundle_digest,
            "matrix_plan_bridge_digest": plan_bridge.digest(),
            "sign_flip_seed": materialized.receipt.sign_flip_seed,
        }
        for name, expected in exact_plan_bindings.items():
            if getattr(plan, name, None) != expected:
                raise ValueError(
                    "formal evaluation plan differs from its current matrix bridge: "
                    f"{name}"
                )
        if plan.stratum.digest() != materialized.stratum.digest():
            raise ValueError("formal cell plan belongs to another materialized stratum")
        expected_pair = {
            materialized.receipt.resolved_focal_identity_digest,
            materialized.receipt.resolved_counterparty_identity_digest,
        }
        if {item.identity_digest() for item in plan.artifacts} != expected_pair:
            raise ValueError("formal cell plan artifacts differ from the projection")
        if len(plan.blocks) != materialized.template.paired_block_count:
            raise ValueError("formal cell plan changed the preregistered block count")
        if plan.bootstrap_seed != materialized.receipt.bootstrap_seed:
            raise ValueError("formal cell plan did not use the future-beacon bootstrap seed")
        if plan.resource_profile.digest() != plan_bridge.resource_profile_digest:
            raise ValueError("formal cell plan changed the bridge resource profile")

        supplied = tuple(paired_blocks)
        by_id = {item.block_id: item for item in supplied}
        if len(by_id) != len(supplied) or set(by_id) != {
            item.block_id for item in plan.blocks
        }:
            raise ValueError("formal cell paired blocks differ from the exact plan")
        ordered = tuple(by_id[item.block_id] for item in plan.blocks)
        aggregate = aggregate_blocks(
            ordered,
            materialized.receipt.resolved_focal_identity_digest,
            plan=plan,
            retry_ledger=retry_ledger,
        )

        observations: dict[Digest, MatchObservation] = {}
        for paired in ordered:
            observations[paired.first.observation_digest()] = paired.first
            observations[paired.swapped.observation_digest()] = paired.swapped
        for entry in retry_ledger.entries:
            observations[entry.original.observation_digest()] = entry.original
            observations[entry.retry.observation_digest()] = entry.retry

        execution_digests: list[Digest] = []
        resource_digests: list[Digest] = []
        raw_digests: list[Digest] = []
        supervisor_contracts: list[Digest] = []
        supervisor_launch_authorizations: list[Digest] = []
        supervisor_leg_receipts: list[Digest] = []
        supervisor_consumption_keys: list[Digest] = []
        supervisor_control_sessions: list[Digest] = []
        supervisor_capture_sessions: list[Digest] = []
        supervisor_socket_identities: list[Digest] = []
        supervisor_wire_semantics: list[Digest] = []
        supervisor_replays: list[Digest] = []
        supervisor_decision_traces: list[Digest] = []
        supervisor_cleanup_receipts: list[Digest] = []
        supervisor_observation_bindings: list[SupervisorObservationBinding] = []
        run_ids: list[Digest] = []
        process_trees: list[str] = []
        cgroup_paths: list[str] = []
        cgroup_inodes: list[int] = []

        def add_typed_raw(domain: str, digest: Digest) -> None:
            """Bind evidence type and bytes without demanding cross-type inequality."""

            raw_digests.append(
                _payload_digest(
                    {
                        "schema": "typed-formal-evidence-reference-v1",
                        "domain": domain,
                        "digest": _digest(digest, f"{domain} evidence"),
                    }
                )
            )

        def required_supervisor_field(
            observation: MatchObservation,
            name: str,
        ) -> Digest:
            # These are typed MatchObservation projections of the exact signed
            # post-run bridge.  Ambiguous aliases or caller-supplied side
            # vectors are deliberately not accepted.
            value = getattr(observation, name, None)
            if value is None:
                raise ValueError(
                    f"formal MatchObservation lacks signed supervisor field {name}"
                )
            return _digest(value, f"signed supervisor {name}")

        def required_supervisor_int(
            observation: MatchObservation,
            name: str,
            *,
            minimum: int,
        ) -> int:
            value = getattr(observation, name, None)
            if type(value) is not int or value < minimum:
                raise ValueError(
                    f"formal MatchObservation lacks signed supervisor integer {name}"
                )
            return value

        def required_supervisor_text(
            observation: MatchObservation,
            name: str,
        ) -> str:
            value = getattr(observation, name, None)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"formal MatchObservation lacks signed supervisor text {name}"
                )
            return value

        for observation_digest in sorted(observations):
            observation = observations[observation_digest]
            add_typed_raw("match-observation", observation_digest)
            supervisor_contracts.append(
                required_supervisor_field(observation, "supervisor_contract_digest")
            )
            supervisor_launch_authorizations.append(
                required_supervisor_field(
                    observation,
                    "supervisor_launch_authorization_digest",
                )
            )
            supervisor_leg_receipts.append(
                required_supervisor_field(
                    observation,
                    "supervisor_leg_receipt_digest",
                )
            )
            supervisor_consumption_keys.append(
                required_supervisor_field(
                    observation,
                    "supervisor_receipt_consumption_key",
                )
            )
            supervisor_control_sessions.append(
                required_supervisor_field(
                    observation,
                    "supervisor_control_session_digest",
                )
            )
            supervisor_capture_sessions.append(
                required_supervisor_field(
                    observation,
                    "supervisor_capture_session_digest",
                )
            )
            socket_identities = getattr(
                observation, "supervisor_socket_identity_digests", None
            )
            if (
                not isinstance(socket_identities, tuple)
                or len(socket_identities) != 2
            ):
                raise ValueError(
                    "formal MatchObservation lacks two signed supervisor socket identities"
                )
            supervisor_socket_identities.extend(
                _digest(value, "signed supervisor socket identity")
                for value in socket_identities
            )
            supervisor_wire_semantics.append(
                required_supervisor_field(
                    observation,
                    "supervisor_wire_semantic_digest",
                )
            )
            supervisor_replays.append(
                required_supervisor_field(
                    observation,
                    "supervisor_replay_digest",
                )
            )
            supervisor_decision_traces.append(
                required_supervisor_field(
                    observation,
                    "supervisor_decision_trace_digest",
                )
            )
            supervisor_cleanup_receipts.append(
                required_supervisor_field(
                    observation,
                    "supervisor_cleanup_receipt_digest",
                )
            )
            supervisor_observation_bindings.append(
                SupervisorObservationBinding(
                    observation_digest=observation_digest,
                    leg_plan_digest=observation.leg_plan.digest(),
                    supervisor_readiness_attestation_digest=(
                        required_supervisor_field(
                            observation,
                            "supervisor_readiness_attestation_digest",
                        )
                    ),
                    supervisor_attempt_journal_scope_digest=(
                        required_supervisor_field(
                            observation,
                            "supervisor_attempt_journal_scope_digest",
                        )
                    ),
                    supervisor_attempt_sequence=required_supervisor_int(
                        observation,
                        "supervisor_attempt_sequence",
                        minimum=1,
                    ),
                    supervisor_previous_attempt_entry_digest=(
                        required_supervisor_field(
                            observation,
                            "supervisor_previous_attempt_entry_digest",
                        )
                    ),
                    supervisor_leg_run_id=required_supervisor_field(
                        observation,
                        "supervisor_leg_run_id",
                    ),
                    supervisor_launch_authorization_digest=(
                        supervisor_launch_authorizations[-1]
                    ),
                    supervisor_leg_receipt_digest=supervisor_leg_receipts[-1],
                    supervisor_receipt_consumption_key=(
                        supervisor_consumption_keys[-1]
                    ),
                    supervisor_consumption_ledger_entry_digest=(
                        required_supervisor_field(
                            observation,
                            "supervisor_consumption_ledger_entry_digest",
                        )
                    ),
                    supervisor_consumption_ledger_entry_inode=(
                        required_supervisor_int(
                            observation,
                            "supervisor_consumption_ledger_entry_inode",
                            minimum=1,
                        )
                    ),
                    supervisor_consumption_ledger_entry_path=(
                        required_supervisor_text(
                            observation,
                            "supervisor_consumption_ledger_entry_path",
                        )
                    ),
                    supervisor_capture_session_digest=(
                        supervisor_capture_sessions[-1]
                    ),
                    supervisor_cleanup_receipt_digest=(
                        supervisor_cleanup_receipts[-1]
                    ),
                    raw_wire_digest=observation.replay_receipt.raw_wire_digest,
                    supervisor_wire_semantic_digest=supervisor_wire_semantics[-1],
                    supervisor_replay_digest=supervisor_replays[-1],
                    replay_verification_digest=(
                        observation.replay_receipt.verification_evidence_digest
                    ),
                )
            )
            for execution in observation.execution_receipts:
                execution_digests.append(execution.digest())
                add_typed_raw("execution-record", execution.raw_evidence_digest)
                add_typed_raw(
                    "execution-termination",
                    execution.termination_evidence_digest,
                )
                run_ids.append(execution.run_id)
                process_trees.append(execution.process_tree_id)
                cgroup_paths.append(execution.cgroup_path)
            for resource in observation.resource_receipts:
                resource_digests.append(resource.digest())
                add_typed_raw("resource-record", resource.raw_evidence_digest)
                cgroup_inodes.append(resource.cgroup_inode)
            replay = observation.replay_receipt
            add_typed_raw("raw-wire", replay.raw_wire_digest)
            add_typed_raw("canonical-replay", replay.raw_replay_digest)
            add_typed_raw("match-trace", replay.match_trace_digest)
            add_typed_raw(
                "replay-verification",
                replay.verification_evidence_digest,
            )
            if replay.attestation_digest is None:
                raise ValueError("formal replay lacks its verifier attestation digest")
            add_typed_raw("replay-attestation", replay.attestation_digest)
            if replay.hand70_evidence_digest is not None:
                add_typed_raw("hand70-evidence", replay.hand70_evidence_digest)
        for entry in retry_ledger.entries:
            add_typed_raw(
                "infrastructure-monitor",
                entry.infrastructure_attribution.raw_monitor_evidence_digest,
            )
            add_typed_raw(
                "infrastructure-attestation",
                entry.infrastructure_attribution.attestation_digest,
            )
        if len(set(raw_digests)) != len(raw_digests):
            raise ValueError("formal cell typed evidence reuses a raw receipt domain")
        if len(set(supervisor_contracts)) != 1:
            raise ValueError("formal cell observations changed supervisor contract")
        expected_attempt_scope = formal_attempt_journal_scope_digest(
            matrix, projection
        )
        if any(
            item.supervisor_attempt_journal_scope_digest
            != expected_attempt_scope
            for item in supervisor_observation_bindings
        ):
            raise ValueError(
                "formal cell observation belongs to another supervisor attempt scope"
            )

        result = cls(
            projection_digest=projection.digest(),
            planned_template_digest=materialized.template.digest(),
            evaluation_stratum_digest=materialized.stratum.digest(),
            seed_cohort_digest=materialized.template.seed_cohort_digest,
            focal_identity_digest=materialized.receipt.resolved_focal_identity_digest,
            counterparty_identity_digest=(
                materialized.receipt.resolved_counterparty_identity_digest
            ),
            formal_plan_digest=plan.digest(),
            aggregate_result_digest=aggregate.digest(),
            deck_sequence_commitment_digests=tuple(
                block.deal_sequence.digest() for block in plan.blocks
            ),
            block_plan_digests=tuple(block.digest() for block in plan.blocks),
            paired_evidence_receipt_digests=tuple(
                _payload_digest(
                    {
                        "block_id": paired.block_id,
                        "first_observation_digest": paired.first.observation_digest(),
                        "swapped_observation_digest": paired.swapped.observation_digest(),
                    }
                )
                for paired in ordered
            ),
            observation_digests=tuple(sorted(observations)),
            execution_receipt_digests=tuple(sorted(execution_digests)),
            resource_receipt_digests=tuple(sorted(resource_digests)),
            raw_evidence_digests=tuple(sorted(raw_digests)),
            supervisor_contract_digest=supervisor_contracts[0],
            supervisor_launch_authorization_digests=tuple(
                sorted(supervisor_launch_authorizations)
            ),
            supervisor_leg_receipt_digests=tuple(
                sorted(supervisor_leg_receipts)
            ),
            supervisor_receipt_consumption_keys=tuple(
                sorted(supervisor_consumption_keys)
            ),
            supervisor_control_session_digests=tuple(
                supervisor_control_sessions
            ),
            supervisor_capture_session_digests=tuple(
                sorted(supervisor_capture_sessions)
            ),
            supervisor_socket_identity_digests=tuple(
                sorted(supervisor_socket_identities)
            ),
            supervisor_wire_semantic_digests=tuple(
                sorted(supervisor_wire_semantics)
            ),
            supervisor_replay_digests=tuple(sorted(supervisor_replays)),
            supervisor_decision_trace_digests=tuple(
                sorted(supervisor_decision_traces)
            ),
            supervisor_cleanup_receipt_digests=tuple(
                sorted(supervisor_cleanup_receipts)
            ),
            supervisor_observation_bindings=tuple(
                supervisor_observation_bindings
            ),
            supervisor_retry_observation_pairs=tuple(
                (
                    entry.original.observation_digest(),
                    entry.retry.observation_digest(),
                )
                for entry in retry_ledger.entries
            ),
            run_ids=tuple(sorted(run_ids)),
            process_tree_ids=tuple(sorted(process_trees)),
            cgroup_paths=tuple(sorted(cgroup_paths)),
            cgroup_inodes=tuple(sorted(cgroup_inodes)),
            focal_score_by_paired_block=tuple(
                paired.candidate_score(
                    materialized.receipt.resolved_focal_identity_digest
                )
                for paired in ordered
            ),
        )
        sealed = result._unchecked_digest()

        def issued_instance(
            candidate: object,
            owner: object = result,
            content_digest: Digest = sealed,
        ) -> bool:
            return (
                candidate is owner
                and isinstance(candidate, FormalCellResult)
                and candidate._unchecked_digest() == content_digest
            )

        object.__setattr__(result, "_verified_token", issued_instance)
        return result

    def _unchecked_digest(self) -> Digest:
        payload = asdict(self)
        payload.pop("_verified_token", None)
        return _payload_digest(payload)

    def _assert_verified(self) -> None:
        guard = self._verified_token
        if not callable(guard) or guard(self) is not True:
            raise ValueError(
                "formal cell result was not derived from typed verified paired-block evidence"
            )

    def digest(self) -> Digest:
        self._assert_verified()
        return self._unchecked_digest()

    def __copy__(self) -> "FormalCellResult":
        payload = asdict(self)
        payload.pop("_verified_token", None)
        return type(self)(**payload)

    def __deepcopy__(self, memo: dict[int, object]) -> "FormalCellResult":
        return self.__copy__()


@dataclass(frozen=True, slots=True)
class NotApplicableAblationReceipt:
    registry_entry_digest: Digest
    route_id: str
    ablation_id: str
    rationale_digest: Digest

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "registry_entry_digest",
            _digest(self.registry_entry_digest, "N/A registry entry"),
        )
        object.__setattr__(
            self,
            "rationale_digest",
            _digest(self.rationale_digest, "N/A rationale"),
        )
        _route_order(self.route_id)
        if self.ablation_id not in _mandatory_ablation_ids(self.route_id):
            raise ValueError("N/A receipt has an unknown ablation key")

    @classmethod
    def from_registration(
        cls, registration: AblationRegistration
    ) -> "NotApplicableAblationReceipt":
        if registration.status != "not-applicable":
            raise ValueError("only explicit N/A registrations receive an N/A receipt")
        return cls(
            registry_entry_digest=registration.digest(),
            route_id=registration.route_id,
            ablation_id=registration.ablation_id,
            rationale_digest=_payload_digest({"rationale": registration.rationale}),
        )

    @property
    def key(self) -> tuple[str, str]:
        return (self.route_id, self.ablation_id)

    def digest(self) -> Digest:
        return _payload_digest(asdict(self))


def formal_attempt_journal_scope_digest(
    matrix: CompleteFormalMatrix,
    projection: FormalMatrixProjection,
) -> Digest:
    """One fixed post-beacon scope for every launch in the complete matrix."""

    return _payload_digest(
        {
            "schema": "complete-formal-matrix-supervisor-attempt-scope-v1",
            "complete_matrix_root_digest": matrix.digest(),
            "projection_digest": projection.digest(),
            "materialized_stratum_digests": tuple(
                item.digest() for item in projection.strata
            ),
        }
    )


def formal_attempt_journal_genesis_digest(scope_digest: Digest) -> Digest:
    """Return the signed journal contract's sequence-one predecessor.

    The external journal deliberately uses the all-zero predecessor so a
    closed seal can prove that it starts at sequence one rather than at a
    caller-selected suffix.  Scope separation is supplied by the signed
    ``attempt_journal_scope_digest`` in every launch, terminal entry, and
    closing seal; accepting any nonzero caller-derived predecessor here would
    make the matrix and supervisor contracts impossible to compose.
    """

    _digest(scope_digest, "attempt journal scope")
    return "0" * 64


@dataclass(frozen=True, slots=True)
class FormalMatrixResultLedger:
    complete_matrix_root_digest: Digest
    projection_digest: Digest
    attempt_journal_scope_digest: Digest
    attempt_journal_seal_digest: Digest
    attempt_journal_entry_digests: tuple[Digest, ...]
    pairwise_hypotheses: tuple[PairwiseHypothesisContract, ...]
    cell_results: tuple[FormalCellResult, ...]
    not_applicable_ablations: tuple[NotApplicableAblationReceipt, ...]
    _ledger_token: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "complete_matrix_root_digest",
            _digest(self.complete_matrix_root_digest, "ledger matrix root"),
        )
        object.__setattr__(
            self,
            "projection_digest",
            _digest(self.projection_digest, "ledger projection"),
        )
        for name in (
            "attempt_journal_scope_digest",
            "attempt_journal_seal_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        entries = tuple(
            _digest(value, "attempt journal entry")
            for value in self.attempt_journal_entry_digests
        )
        if not entries or len(set(entries)) != len(entries):
            raise ValueError("attempt journal entries must be non-empty and unique")
        object.__setattr__(self, "attempt_journal_entry_digests", entries)

    def digest(self) -> Digest:
        return _payload_digest(
            {
                "schema": "formal-matrix-result-ledger-v1",
                "complete_matrix_root_digest": self.complete_matrix_root_digest,
                "projection_digest": self.projection_digest,
                "attempt_journal_scope_digest": self.attempt_journal_scope_digest,
                "attempt_journal_seal_digest": self.attempt_journal_seal_digest,
                "attempt_journal_entry_digests": self.attempt_journal_entry_digests,
                "pairwise_hypothesis_digests": tuple(
                    item.digest() for item in self.pairwise_hypotheses
                ),
                "cell_result_digests": tuple(item.digest() for item in self.cell_results),
                "not_applicable_receipt_digests": tuple(
                    item.digest() for item in self.not_applicable_ablations
                ),
            }
        )

    def __copy__(self) -> "FormalMatrixResultLedger":
        return type(self)(
            complete_matrix_root_digest=self.complete_matrix_root_digest,
            projection_digest=self.projection_digest,
            attempt_journal_scope_digest=self.attempt_journal_scope_digest,
            attempt_journal_seal_digest=self.attempt_journal_seal_digest,
            attempt_journal_entry_digests=self.attempt_journal_entry_digests,
            pairwise_hypotheses=self.pairwise_hypotheses,
            cell_results=self.cell_results,
            not_applicable_ablations=self.not_applicable_ablations,
        )

    def __deepcopy__(self, memo: dict[int, object]) -> "FormalMatrixResultLedger":
        return type(self)(
            complete_matrix_root_digest=self.complete_matrix_root_digest,
            projection_digest=self.projection_digest,
            attempt_journal_scope_digest=self.attempt_journal_scope_digest,
            attempt_journal_seal_digest=self.attempt_journal_seal_digest,
            attempt_journal_entry_digests=copy.deepcopy(
                self.attempt_journal_entry_digests, memo
            ),
            pairwise_hypotheses=copy.deepcopy(self.pairwise_hypotheses, memo),
            cell_results=copy.deepcopy(self.cell_results, memo),
            not_applicable_ablations=copy.deepcopy(
                self.not_applicable_ablations, memo
            ),
        )

    def assert_for(
        self,
        matrix: CompleteFormalMatrix,
        projection: FormalMatrixProjection,
    ) -> None:
        matrix.assert_formal_authority()
        if self._ledger_token is not _LEDGER_AUTHORITY:
            raise ValueError("result ledger lacks complete global coverage authority")
        if self.complete_matrix_root_digest != matrix.digest():
            raise ValueError("result ledger belongs to another complete matrix")
        if self.projection_digest != projection.digest():
            raise ValueError("result ledger belongs to another post-beacon projection")
        if self.attempt_journal_scope_digest != formal_attempt_journal_scope_digest(
            matrix, projection
        ):
            raise ValueError("result ledger belongs to another attempt-journal scope")
        if tuple(item.digest() for item in self.pairwise_hypotheses) != tuple(
            item.digest() for item in matrix.pairwise_hypotheses
        ):
            raise ValueError("result ledger changed the frozen pairwise hypothesis family")

    def result_for(self, planned_template_digest: Digest) -> FormalCellResult:
        digest = _digest(planned_template_digest, "planned template")
        matches = [item for item in self.cell_results if item.planned_template_digest == digest]
        if len(matches) != 1:
            raise KeyError(digest)
        return matches[0]

    def paired_difference(
        self,
        left_template_digest: Digest,
        right_template_digest: Digest,
    ) -> tuple[float, ...]:
        """Return left-minus-right on one exact shared cohort.

        Reversing the arguments is mathematically guaranteed to return the
        element-wise negative vector; no separately oriented duplicate result
        may be inserted into the ledger.
        """

        left = self.result_for(left_template_digest)
        right = self.result_for(right_template_digest)
        left._assert_verified()
        right._assert_verified()
        if left.seed_cohort_digest != right.seed_cohort_digest:
            raise ValueError("paired difference requires one common seed cohort")
        if (
            left.deck_sequence_commitment_digests
            != right.deck_sequence_commitment_digests
        ):
            raise ValueError("paired difference requires identical committed 70-deal sequences")
        if len(left.focal_score_by_paired_block) != len(right.focal_score_by_paired_block):
            raise ValueError("paired difference requires identical block coverage")
        return tuple(
            left_score - right_score
            for left_score, right_score in zip(
                left.focal_score_by_paired_block,
                right.focal_score_by_paired_block,
                strict=True,
            )
        )

    def direct_paired_difference(
        self,
        template_digest: Digest,
        requested_focal_identity_digest: Digest,
    ) -> tuple[float, ...]:
        result = self.result_for(template_digest)
        result._assert_verified()
        requested = _digest(requested_focal_identity_digest, "requested direct focal")
        if requested == result.focal_identity_digest:
            sign = 1.0
        elif requested == result.counterparty_identity_digest:
            sign = -1.0
        else:
            raise ValueError("requested focal identity is absent from the direct cell")
        return tuple(sign * (2.0 * score - 1.0) for score in result.focal_score_by_paired_block)

    def hypothesis_difference(
        self,
        hypothesis_id: Digest,
        *,
        reverse: bool = False,
    ) -> tuple[float, ...]:
        """Resolve one frozen claim; ``reverse`` returns the exact negation."""

        hypothesis_id = _digest(hypothesis_id, "pairwise hypothesis")
        matches = [
            item
            for item in self.pairwise_hypotheses
            if item.hypothesis_id == hypothesis_id
        ]
        if len(matches) != 1:
            raise KeyError(hypothesis_id)
        hypothesis = matches[0]
        if hypothesis.right_template_digest is None:
            values = self.direct_paired_difference(
                hypothesis.left_template_digest,
                hypothesis.left_identity_digest,
            )
        else:
            values = self.paired_difference(
                hypothesis.left_template_digest,
                hypothesis.right_template_digest,
            )
        return tuple(-value for value in values) if reverse else values


def validate_attempt_journal_leg_policy(
    *,
    entries: Sequence[object],
    bindings: Sequence[SupervisorObservationBinding],
    max_retries_by_leg: Mapping[Digest, int],
    retry_observation_pairs: Sequence[tuple[Digest, Digest]],
) -> None:
    """Purely validate per-Leg attempt selection and retry accounting.

    Signature/durable-ledger authority is deliberately outside this helper.
    Its inputs are the already retained terminal journal rows, typed
    observation bindings, frozen per-Leg retry limits, and retry edges emitted
    by the verified :class:`RetryLedger`.  Keeping the state machine pure makes
    the anti-selection rules independently testable without creating a local
    formal-authority mint.

    A failed attempt without an observation cannot distinguish an early
    candidate failure from bot-independent infrastructure and therefore makes
    the formal scope fail closed; a signed terminal-state label alone is not
    retry authority.  A replay-bearing failed attempt can only be followed when
    the exact adjacent observed edge is present in the verified retry ledger;
    this prevents a completed/candidate-fault result from being relabelled and
    discarded.
    """

    rows = tuple(entries)
    retained = tuple(bindings)
    if not rows or not retained:
        raise ValueError("attempt policy requires journal rows and observations")

    bindings_by_leg: dict[Digest, list[SupervisorObservationBinding]] = {}
    binding_by_replay: dict[Digest, SupervisorObservationBinding] = {}
    observation_digests: set[Digest] = set()
    for binding in retained:
        leg = _digest(binding.leg_plan_digest, "retained LegPlan")
        replay_verification = _digest(
            binding.replay_verification_digest,
            "retained replay verification",
        )
        observation = _digest(
            binding.observation_digest,
            "retained observation",
        )
        if observation in observation_digests:
            raise ValueError("attempt policy reuses a retained observation")
        if replay_verification in binding_by_replay:
            raise ValueError("attempt policy reuses replay verification evidence")
        observation_digests.add(observation)
        binding_by_replay[replay_verification] = binding
        bindings_by_leg.setdefault(leg, []).append(binding)

    limits: dict[Digest, int] = {}
    for raw_leg, limit in max_retries_by_leg.items():
        leg = _digest(raw_leg, "retry-limit LegPlan")
        if type(limit) is not int or limit < 0:
            raise ValueError("attempt policy retry limits must be non-negative integers")
        if leg in limits:
            raise ValueError("attempt policy repeats a LegPlan retry limit")
        limits[leg] = limit
    if set(limits) != set(bindings_by_leg):
        raise ValueError(
            "journal legal Leg set differs from retained SupervisorObservationBindings"
        )

    rows_by_leg: dict[Digest, list[object]] = {}
    seen_attempt_sequences: set[int] = set()
    for row in rows:
        leg = _digest(getattr(row, "leg_plan_digest", None), "journal LegPlan")
        sequence = getattr(row, "attempt_sequence", None)
        if type(sequence) is not int or sequence < 1:
            raise ValueError("journal attempt sequence must be positive")
        if sequence in seen_attempt_sequences:
            raise ValueError("journal attempt sequence is reused")
        seen_attempt_sequences.add(sequence)
        rows_by_leg.setdefault(leg, []).append(row)
    if set(rows_by_leg) != set(bindings_by_leg):
        raise ValueError(
            "journal Leg set differs from retained SupervisorObservationBindings"
        )

    normalized_retry_pairs: set[tuple[Digest, Digest]] = set()
    for pair in retry_observation_pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("retry observation edge must contain two digests")
        original = _digest(pair[0], "retry original observation")
        retry = _digest(pair[1], "retry observation")
        edge = (original, retry)
        if original == retry or edge in normalized_retry_pairs:
            raise ValueError("retry observation edges must be unique and directed")
        if original not in observation_digests or retry not in observation_digests:
            raise ValueError("retry edge references an unretained observation")
        normalized_retry_pairs.add(edge)

    allowed_failed_states = {
        "launch_failed",
        "capture_failed",
        "cleanup_failed",
        "infrastructure_failed",
    }
    expected_retry_pairs: set[tuple[Digest, Digest]] = set()
    journal_replay_verifications: set[Digest] = set()
    for leg, unsorted_leg_rows in rows_by_leg.items():
        leg_rows = tuple(
            sorted(
                unsorted_leg_rows,
                key=lambda item: getattr(item, "attempt_sequence"),
            )
        )
        if len(leg_rows) - 1 > limits[leg]:
            raise ValueError(
                "journal Leg exceeds its preregistered infrastructure retry cap"
            )
        states = tuple(getattr(item, "terminal_state", None) for item in leg_rows)
        if "aborted" in states:
            raise ValueError("aborted supervisor attempt permanently invalidates the scope")
        completed_indexes = tuple(
            index for index, state in enumerate(states) if state == "completed"
        )
        if completed_indexes != (len(leg_rows) - 1,):
            raise ValueError(
                "each journal Leg must end in exactly one completed observation"
            )
        if any(state not in allowed_failed_states for state in states[:-1]):
            raise ValueError(
                "only explicit infrastructure failures may precede completion"
            )

        observed: list[Digest] = []
        for row in leg_rows:
            replay = getattr(row, "replay_digest", None)
            replay_verification = getattr(
                row, "replay_verification_digest", None
            )
            if (replay is None) != (replay_verification is None):
                raise ValueError(
                    "replay-bearing failed attempt lacks complete observation evidence"
                )
            if replay_verification is None:
                if getattr(row, "terminal_state", None) == "completed":
                    raise ValueError(
                        "completed supervisor attempt lacks a retained observation"
                    )
                raise ValueError(
                    "pre-replay failure lacks independent candidate-versus-"
                    "infrastructure attribution; formal scope fails closed"
                )
            replay_verification = _digest(
                replay_verification, "journal replay verification"
            )
            if replay_verification in journal_replay_verifications:
                raise ValueError("journal reuses replay verification evidence")
            journal_replay_verifications.add(replay_verification)
            binding = binding_by_replay.get(replay_verification)
            if binding is None or binding.leg_plan_digest != leg:
                raise ValueError(
                    "replay-bearing journal attempt lacks its retained observation"
                )
            observed.append(binding.observation_digest)
        expected_retry_pairs.update(zip(observed, observed[1:], strict=False))

    if journal_replay_verifications != set(binding_by_replay):
        raise ValueError(
            "journal and retained observations differ; an attempt was discarded"
        )
    if normalized_retry_pairs != expected_retry_pairs:
        raise ValueError(
            "replay-bearing failed attempts lack the exact verified RetryLedger chain"
        )


def _validate_closed_attempt_journal(
    matrix: CompleteFormalMatrix,
    projection: FormalMatrixProjection,
    results: Sequence[FormalCellResult],
    attempt_journal: object,
) -> tuple[Digest, Digest, tuple[Digest, ...]]:
    from .resource_enforcer import AuthorizedSupervisorAttemptJournal

    if not isinstance(attempt_journal, AuthorizedSupervisorAttemptJournal):
        raise ValueError(
            "formal matrix result requires a typed authorized supervisor attempt journal"
        )
    attempt_journal._assert_authorized()
    scope = formal_attempt_journal_scope_digest(matrix, projection)
    if attempt_journal.scope_digest != scope:
        raise ValueError("supervisor attempt journal belongs to another matrix scope")
    entries = tuple(attempt_journal.entries)
    if (
        attempt_journal.first_attempt_sequence != 1
        or attempt_journal.last_attempt_sequence != len(entries)
        or attempt_journal.entry_count != len(entries)
        or tuple(item.attempt_sequence for item in entries)
        != tuple(range(1, len(entries) + 1))
    ):
        raise ValueError("formal attempt journal must be a complete sequence from one")
    if entries[0].previous_attempt_entry_digest != (
        formal_attempt_journal_genesis_digest(scope)
    ):
        raise ValueError("formal attempt journal lacks its scope-bound genesis")
    if tuple(item.payload_digest() for item in entries) != (
        attempt_journal.entry_digests
    ):
        raise ValueError("formal attempt journal entry vector changed after authorization")

    # Reject reuse in every authority-bearing domain, including attempts that
    # failed before a replay observation existed.
    unique_optional_fields = (
        "launch_authorization_digest",
        "supervisor_leg_receipt_digest",
        "capture_session_digest",
        "receipt_consumption_key",
        "cleanup_receipt_digest",
        "raw_wire_digest",
        "wire_semantic_digest",
        "replay_digest",
        "replay_verification_digest",
    )
    for name in unique_optional_fields:
        values = tuple(
            value
            for value in (getattr(item, name) for item in entries)
            if value is not None
        )
        if not values or len(set(values)) != len(values):
            raise ValueError(
                f"formal attempt journal reuses or omits {name.replace('_', ' ')}"
            )

    bindings = tuple(
        binding
        for result in results
        for binding in result.supervisor_observation_bindings
    )
    if len({item.observation_digest for item in bindings}) != len(bindings):
        raise ValueError("formal result ledger reuses an observation binding")
    by_replay_verification = {
        item.replay_verification_digest: item for item in bindings
    }
    if len(by_replay_verification) != len(bindings):
        raise ValueError("formal result ledger reuses replay verification evidence")
    journal_observed = {
        item.replay_verification_digest: item
        for item in entries
        if item.replay_verification_digest is not None
    }
    if len(journal_observed) != sum(
        item.replay_verification_digest is not None for item in entries
    ):
        raise ValueError("formal attempt journal reuses replay verification evidence")
    if set(journal_observed) != set(by_replay_verification):
        raise ValueError(
            "closed attempt journal and retained MatchObservations differ; "
            "an attempt was omitted, injected, or selectively discarded"
        )
    for replay_digest, binding in by_replay_verification.items():
        entry = journal_observed[replay_digest]
        if not binding.matches_attempt_entry(entry):
            raise ValueError(
                "signed attempt journal row differs from its typed MatchObservation"
            )

    template_by_digest = {
        item.template.digest(): item.template for item in projection.strata
    }
    max_retries_by_leg: dict[Digest, int] = {}
    leg_to_template: dict[Digest, Digest] = {}
    retry_observation_pairs: list[tuple[Digest, Digest]] = []
    for result in results:
        template = template_by_digest.get(result.planned_template_digest)
        if template is None:
            raise ValueError(
                "attempt journal result belongs to an unknown planned template"
            )
        retry_observation_pairs.extend(
            result.supervisor_retry_observation_pairs
        )
        for binding in result.supervisor_observation_bindings:
            prior_template = leg_to_template.setdefault(
                binding.leg_plan_digest,
                result.planned_template_digest,
            )
            if prior_template != result.planned_template_digest:
                raise ValueError(
                    "one journal LegPlan is ambiguously shared across matrix cells"
                )
            max_retries_by_leg[binding.leg_plan_digest] = (
                template.max_infrastructure_retries_per_leg
            )
    validate_attempt_journal_leg_policy(
        entries=entries,
        bindings=bindings,
        max_retries_by_leg=max_retries_by_leg,
        retry_observation_pairs=retry_observation_pairs,
    )

    binding_unique_fields = (
        "supervisor_leg_run_id",
        "supervisor_consumption_ledger_entry_digest",
        "supervisor_consumption_ledger_entry_inode",
        "supervisor_consumption_ledger_entry_path",
    )
    for name in binding_unique_fields:
        values = tuple(getattr(item, name) for item in bindings)
        if not values or len(set(values)) != len(values):
            raise ValueError(
                "formal result ledger reuses or omits "
                f"{name.replace('_', ' ')}"
            )
    completed_without_observation = [
        item.attempt_sequence
        for item in entries
        if item.terminal_state == "completed"
        and item.replay_verification_digest is None
    ]
    if completed_without_observation:
        raise ValueError("completed supervisor attempts lack retained observations")

    contracts = {result.supervisor_contract_digest for result in results}
    if contracts != {attempt_journal.contract_digest}:
        raise ValueError("attempt journal and cell results use different supervisors")
    return (
        scope,
        attempt_journal.seal_digest,
        tuple(attempt_journal.entry_digests),
    )


def build_formal_matrix_result_ledger(
    matrix: CompleteFormalMatrix,
    projection: FormalMatrixProjection,
    cell_results: Sequence[FormalCellResult],
    not_applicable_ablations: Sequence[NotApplicableAblationReceipt],
    attempt_journal: object,
) -> FormalMatrixResultLedger:
    matrix.assert_formal_authority()
    if projection.complete_matrix_root_digest != matrix.digest():
        raise ValueError("projection belongs to another complete matrix")
    expected_by_template = {item.template.digest(): item for item in projection.strata}
    results = tuple(sorted(cell_results, key=lambda item: item.planned_template_digest))
    for result in results:
        result._assert_verified()
    if len(results) != len(expected_by_template) or {
        item.planned_template_digest for item in results
    } != set(expected_by_template):
        raise ValueError("result ledger must exactly cover every formal matrix cell once")
    globally_unique_fields = (
        "block_plan_digests",
        "paired_evidence_receipt_digests",
        "observation_digests",
        "execution_receipt_digests",
        "resource_receipt_digests",
        "raw_evidence_digests",
        "supervisor_launch_authorization_digests",
        "supervisor_leg_receipt_digests",
        "supervisor_receipt_consumption_keys",
        "supervisor_capture_session_digests",
        "supervisor_socket_identity_digests",
        "supervisor_wire_semantic_digests",
        "supervisor_replay_digests",
        "supervisor_decision_trace_digests",
        "supervisor_cleanup_receipt_digests",
        "run_ids",
        "process_tree_ids",
        "cgroup_paths",
        "cgroup_inodes",
    )
    globally_consumed: dict[str, set[object]] = {
        name: set() for name in globally_unique_fields
    }
    decks_by_cohort: dict[Digest, tuple[Digest, ...]] = {}
    supervisor_contract_digest: Digest | None = None
    direct_keys: set[tuple[object, ...]] = set()
    for result in results:
        materialized = expected_by_template[result.planned_template_digest]
        if result.projection_digest != projection.digest():
            raise ValueError("cell result belongs to another projection")
        if result.evaluation_stratum_digest != materialized.stratum.digest():
            raise ValueError("cell result belongs to another evaluation stratum")
        if result.seed_cohort_digest != materialized.template.seed_cohort_digest:
            raise ValueError("cell result changed its common seed cohort")
        if result.focal_identity_digest != materialized.receipt.resolved_focal_identity_digest:
            raise ValueError("cell result changed its preregistered focal orientation")
        if result.counterparty_identity_digest != (
            materialized.receipt.resolved_counterparty_identity_digest
        ):
            raise ValueError("cell result changed its preregistered counterparty")
        if len(result.block_plan_digests) != materialized.template.paired_block_count:
            raise ValueError("cell result does not cover the exact preregistered block count")
        if supervisor_contract_digest is None:
            supervisor_contract_digest = result.supervisor_contract_digest
        elif result.supervisor_contract_digest != supervisor_contract_digest:
            raise ValueError("formal matrix changed its fixed supervisor contract")
        prior_decks = decks_by_cohort.setdefault(
            result.seed_cohort_digest,
            result.deck_sequence_commitment_digests,
        )
        if prior_decks != result.deck_sequence_commitment_digests:
            raise ValueError(
                "formal cells in one seed cohort used different committed deal sequences"
            )
        for name, consumed in globally_consumed.items():
            values = set(getattr(result, name))
            if consumed & values:
                raise ValueError(
                    "formal matrix globally reused one "
                    f"{name.replace('_', ' ')} value"
                )
            consumed.update(values)
        key = materialized.template.key
        if key.kind == "direct-h2h":
            symmetric_key = (
                frozenset(
                    {
                        result.focal_identity_digest,
                        result.counterparty_identity_digest,
                    }
                ),
                key.checkpoint,
                key.comparison_mode,
                key.budget_ms,
            )
            if symmetric_key in direct_keys:
                raise ValueError("direct H2H result is duplicated in reverse orientation")
            direct_keys.add(symmetric_key)

    (
        attempt_scope_digest,
        attempt_seal_digest,
        attempt_entry_digests,
    ) = _validate_closed_attempt_journal(
        matrix,
        projection,
        results,
        attempt_journal,
    )

    expected_na = {
        item.key: item for item in matrix.ablation_registry.not_applicable
    }
    na = tuple(sorted(not_applicable_ablations, key=lambda item: item.key))
    if len(na) != len(expected_na) or {item.key for item in na} != set(expected_na):
        raise ValueError("result ledger must explicitly acknowledge every N/A ablation")
    for receipt in na:
        registration = expected_na[receipt.key]
        if receipt.registry_entry_digest != registration.digest() or receipt.rationale_digest != (
            _payload_digest({"rationale": registration.rationale})
        ):
            raise ValueError("N/A receipt differs from the frozen ablation registry")
    ledger = FormalMatrixResultLedger(
        complete_matrix_root_digest=matrix.digest(),
        projection_digest=projection.digest(),
        attempt_journal_scope_digest=attempt_scope_digest,
        attempt_journal_seal_digest=attempt_seal_digest,
        attempt_journal_entry_digests=attempt_entry_digests,
        pairwise_hypotheses=matrix.pairwise_hypotheses,
        cell_results=results,
        not_applicable_ablations=na,
    )
    object.__setattr__(ledger, "_ledger_token", _LEDGER_AUTHORITY)
    ledger.assert_for(matrix, projection)
    return ledger


@dataclass(frozen=True, slots=True)
class LegacyRouteArtifacts:
    route_id: str
    best: ArtifactIdentity
    controlled: ArtifactIdentity

    def __post_init__(self) -> None:
        _route_order(self.route_id)
        if self.best.identity_digest() == self.controlled.identity_digest():
            raise ValueError("legacy diagnostic aliases are not two artifacts")


@dataclass(frozen=True, slots=True)
class LegacyDiagnosticMatrix:
    routes: tuple[LegacyRouteArtifacts, ...]
    reason: str
    result_authority: str = "development_diagnostic_only"

    def __post_init__(self) -> None:
        routes = tuple(sorted(self.routes, key=lambda item: _route_order(item.route_id)))
        if tuple(item.route_id for item in routes) != ROUTE_IDS:
            raise ValueError("legacy diagnostic matrix still requires A1/A2/B")
        if self.result_authority != "development_diagnostic_only":
            raise ValueError("legacy six-artifact matrix can never gain formal authority")
        if not self.reason:
            raise ValueError("legacy diagnostic matrix requires an explicit reason")
        object.__setattr__(self, "routes", routes)

    def assert_formal_authority(self) -> None:
        raise ValueError(
            "legacy six-artifact matrix is diagnostic only; build CompleteFormalMatrix v2"
        )


def build_legacy_diagnostic_matrix(
    routes: Sequence[LegacyRouteArtifacts], *, reason: str
) -> LegacyDiagnosticMatrix:
    """Explicit compatibility reader; never accepted by formal projection."""

    return LegacyDiagnosticMatrix(tuple(routes), reason)


__all__ = [
    "ABLATION_PAIRED_BLOCKS",
    "BOOTSTRAP_SAMPLES",
    "CHECKPOINT_IDS",
    "COMMON_ABLATION_IDS",
    "COMPARISON_MODES",
    "DIRECT_PAIRED_BLOCKS",
    "EXTERNAL_PAIRED_BLOCKS",
    "FIXED_OPPONENT_ROLES",
    "FORMAL_BUDGETS_MS",
    "HELDOUT_SLOT_IDS",
    "ROUTE_IDS",
    "ROUTE_SPECIFIC_ABLATION_IDS",
    "AblationRegistration",
    "BudgetProfile",
    "CheckpointFreezeReceipt",
    "CompleteFormalMatrix",
    "FormalCellResult",
    "FormalMatrixProjection",
    "FormalMatrixResultLedger",
    "FrozenOpponentFamily",
    "HeldoutPrecommitment",
    "HeldoutReveal",
    "HeldoutSelectionReceipt",
    "HolmFamilyContract",
    "LegacyDiagnosticMatrix",
    "LegacyRouteArtifacts",
    "MainArtifact",
    "MandatoryAblationRegistry",
    "MaterializedFormalMatrix",
    "MaterializedStratum",
    "MaterializedStratumReceipt",
    "MatrixCellKey",
    "MatrixSharedContracts",
    "NotApplicableAblationReceipt",
    "OpponentSplitFreeze",
    "PairwiseHypothesisContract",
    "PlannedStratumTemplate",
    "RouteArtifacts",
    "build_complete_formal_matrix",
    "build_formal_matrix_result_ledger",
    "build_legacy_diagnostic_matrix",
    "create_heldout_precommitment",
    "materialize_formal_matrix",
    "materialize_diagnostic_matrix_projection",
    "validate_attempt_journal_leg_policy",
]
