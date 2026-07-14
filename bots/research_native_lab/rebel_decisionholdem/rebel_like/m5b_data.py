"""Pre-label split closure and content-addressed M5b dataset shards."""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import math
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

from ...common_contracts.national_state import NationalGameState
from .hunl_pbs import HUNL_COMBO_COUNT, HUNLReachFactorPublicBeliefState
from .m5b_contract import (
    M5B_SAMPLE_SCHEMA,
    M5B_SOLVER_NAME,
    M5B_SPLIT_SCHEMA,
    canonical_bytes,
    sha256_bytes,
)
from .m5b_search import ACTION_COUNT, AbstractActionSet, PrivateTargets


ROUTE_DOMAIN_SALT = "route-a1-m5b-only-v1"
EDGE_NAMESPACES = (
    "same_canonical_public_family",
    "same_trajectory",
    "same_rollout_group",
    "same_augmentation_parent",
    "same_source_sample_checkpoint_identity",
    "duplicate_mathematical_pbs",
)
SPLITS = ("train", "validation", "test")
MAX_HISTORY_ACTIONS = 32
PUBLIC_FEATURE_DIM = 474
GENERATOR_RECEIPT_SCHEMA = "route-a1-m5b-label-generator-receipt-v1"
GENERATOR_RECEIPT_FIELDS = {
    "schema",
    "route",
    "solver",
    "sample_id",
    "pbs_network_input_sha256",
    "split_manifest_sha256",
    "split_authority_sha256",
    "config_sha256",
    "source_checkpoint_sha256",
    "search_result_sha256",
    "solver_seed",
    "target_seed",
    "source_files_sha256",
    "source_closure_sha256",
    "split_authority_source_closure_sha256",
    "python_implementation",
    "python_version",
    "numpy_version",
    "array_content_sha256",
    "weighted_zero_sum_residual_chips",
    "labels_generated_after_split",
    "a2_artifact_used",
    "route_b_model_or_data_used",
    "q_cfv_training_authority",
}


def _reject_symlink_components(path: Path) -> None:
    absolute = Path(path) if Path(path).is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise ValueError(f"artifact path contains a symlink: {current}")


def _stable_regular_bytes(path: Path, *, label: str) -> bytes:
    path = Path(path)
    _reject_symlink_components(path)
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ValueError(f"{label} is a symlink or not a regular file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    chunks: list[bytes] = []
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"{label} changed during secure open")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError(f"{label} changed during stable read")
        _reject_symlink_components(path)
        final = path.lstat()
        if (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"{label} path changed during stable read")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _stable_regular_sha256(path: Path) -> str:
    return hashlib.sha256(
        _stable_regular_bytes(path, label="generator source")
    ).hexdigest()


def label_generator_source_files_sha256() -> dict[str, str]:
    route_root = Path(__file__).resolve().parents[1]
    research_root = route_root.parent
    sources = {
        "rebel_like/m5b_data.py": route_root / "rebel_like" / "m5b_data.py",
        "rebel_like/m5b_search.py": route_root / "rebel_like" / "m5b_search.py",
        "rebel_like/m5b_contract.py": route_root / "rebel_like" / "m5b_contract.py",
        "rebel_like/hunl_pbs.py": route_root / "rebel_like" / "hunl_pbs.py",
        "common_contracts/actions.py": research_root / "common_contracts" / "actions.py",
        "common_contracts/cards.py": research_root / "common_contracts" / "cards.py",
        "common_contracts/constants.py": (
            research_root / "common_contracts" / "constants.py"
        ),
        "common_contracts/national_state.py": (
            research_root / "common_contracts" / "national_state.py"
        ),
    }
    return {
        label: _stable_regular_sha256(path) for label, path in sorted(sources.items())
    }


def _is_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _digest(payload: object) -> str:
    return sha256_bytes(canonical_bytes(payload))


def _python_version() -> str:
    return ".".join(str(part) for part in sys.version_info[:3])


def _build_generator_receipt(
    *,
    plan: "PreLabelPlan",
    pbs_network_input_sha256: str,
    split_manifest_sha256: str,
    split_authority_sha256: str,
    split_authority_source_closure_sha256: str,
    generator_config_sha256: str,
    search_result_sha256: str,
    solver_seed: int,
    target_seed: int,
    array_content_sha256: str,
    weighted_zero_sum_residual_chips: float,
) -> dict[str, object]:
    digest_fields = {
        "pbs_network_input_sha256": pbs_network_input_sha256,
        "split_manifest_sha256": split_manifest_sha256,
        "split_authority_sha256": split_authority_sha256,
        "split_authority_source_closure_sha256": (
            split_authority_source_closure_sha256
        ),
        "generator_config_sha256": generator_config_sha256,
        "search_result_sha256": search_result_sha256,
        "array_content_sha256": array_content_sha256,
    }
    for label, value in digest_fields.items():
        if not _is_digest(value):
            raise ValueError(f"generator receipt {label} is invalid")
    for label, value in (("solver_seed", solver_seed), ("target_seed", target_seed)):
        if type(value) is not int or value < 0:
            raise ValueError(f"generator receipt {label} is invalid")
    if (
        type(weighted_zero_sum_residual_chips) is not float
        or not math.isfinite(weighted_zero_sum_residual_chips)
        or abs(weighted_zero_sum_residual_chips) > 1e-5
    ):
        raise ValueError("generator receipt weighted zero-sum residual is invalid")
    source_files = label_generator_source_files_sha256()
    return {
        "schema": GENERATOR_RECEIPT_SCHEMA,
        "route": "A1",
        "solver": M5B_SOLVER_NAME,
        "sample_id": plan.sample_id,
        "pbs_network_input_sha256": pbs_network_input_sha256,
        "split_manifest_sha256": split_manifest_sha256,
        "split_authority_sha256": split_authority_sha256,
        "config_sha256": generator_config_sha256,
        "source_checkpoint_sha256": plan.source_checkpoint_digest,
        "search_result_sha256": search_result_sha256,
        "solver_seed": solver_seed,
        "target_seed": target_seed,
        "source_files_sha256": source_files,
        "source_closure_sha256": _digest(source_files),
        "split_authority_source_closure_sha256": (
            split_authority_source_closure_sha256
        ),
        "python_implementation": sys.implementation.name,
        "python_version": _python_version(),
        "numpy_version": np.__version__,
        "array_content_sha256": array_content_sha256,
        "weighted_zero_sum_residual_chips": weighted_zero_sum_residual_chips,
        "labels_generated_after_split": True,
        "a2_artifact_used": False,
        "route_b_model_or_data_used": False,
        "q_cfv_training_authority": False,
    }


def validate_generator_receipt(
    receipt: object,
    *,
    expected_sample_id: str,
    expected_pbs_network_input_sha256: str,
    expected_split_manifest_sha256: str,
    expected_split_authority_sha256: str,
    expected_split_authority_source_closure_sha256: str,
    expected_generator_config_sha256: str,
    expected_source_checkpoint_sha256: str,
    expected_array_content_sha256: str,
    weighted_zero_sum_residual_chips: float,
) -> dict[str, object]:
    """Verify label provenance against independently supplied authorities."""

    expected_digests = {
        "sample_id": expected_sample_id,
        "pbs_network_input_sha256": expected_pbs_network_input_sha256,
        "split_manifest_sha256": expected_split_manifest_sha256,
        "split_authority_sha256": expected_split_authority_sha256,
        "split_authority_source_closure_sha256": (
            expected_split_authority_source_closure_sha256
        ),
        "config_sha256": expected_generator_config_sha256,
        "source_checkpoint_sha256": expected_source_checkpoint_sha256,
        "array_content_sha256": expected_array_content_sha256,
    }
    if any(not _is_digest(value) for value in expected_digests.values()):
        raise ValueError("generator receipt external digest authority is invalid")
    if type(receipt) is not dict or set(receipt) != GENERATOR_RECEIPT_FIELDS:
        raise ValueError("dataset generator receipt exact schema differs")
    for field, expected in expected_digests.items():
        if receipt[field] != expected:
            raise ValueError(f"dataset generator receipt {field} binding differs")
    if (
        receipt["schema"] != GENERATOR_RECEIPT_SCHEMA
        or receipt["route"] != "A1"
        or receipt["solver"] != M5B_SOLVER_NAME
        or receipt["labels_generated_after_split"] is not True
        or receipt["a2_artifact_used"] is not False
        or receipt["route_b_model_or_data_used"] is not False
        or receipt["q_cfv_training_authority"] is not False
    ):
        raise ValueError("dataset generator receipt label authority differs")
    for field in ("search_result_sha256", "source_closure_sha256"):
        if not _is_digest(receipt[field]):
            raise ValueError(f"dataset generator receipt {field} is invalid")
    for field in ("solver_seed", "target_seed"):
        if type(receipt[field]) is not int or receipt[field] < 0:
            raise ValueError(f"dataset generator receipt {field} is invalid")
    source_files = label_generator_source_files_sha256()
    if (
        receipt["source_files_sha256"] != source_files
        or receipt["source_closure_sha256"] != _digest(source_files)
    ):
        raise ValueError("dataset generator source closure differs")
    if (
        receipt["python_implementation"] != sys.implementation.name
        or receipt["python_version"] != _python_version()
        or receipt["numpy_version"] != np.__version__
    ):
        raise ValueError("dataset generator numeric runtime differs")
    claimed_residual = receipt["weighted_zero_sum_residual_chips"]
    if (
        type(claimed_residual) is not float
        or not math.isfinite(claimed_residual)
        or abs(claimed_residual) > 1e-5
        or claimed_residual != weighted_zero_sum_residual_chips
    ):
        raise ValueError("dataset generator weighted zero-sum receipt differs")
    return dict(receipt)


def canonical_board_family(board: Sequence[int]) -> tuple[tuple[int, int], ...]:
    cards = tuple(int(card) for card in board)
    if any(not 0 <= card < 52 for card in cards) or len(set(cards)) != len(cards):
        raise ValueError("public board is invalid")
    candidates: list[tuple[tuple[int, int], ...]] = []
    for permutation in itertools.permutations(range(4)):
        mapped = tuple((card // 4, permutation[card % 4]) for card in cards)
        if len(mapped) >= 3:
            mapped = tuple(sorted(mapped[:3])) + mapped[3:]
        candidates.append(mapped)
    return min(candidates, default=())


def encode_public_features(public: Mapping[str, object]) -> np.ndarray:
    """Encode only Common public fields; complete action order is retained."""

    forbidden = {"hole_cards", "hand_number", "match_net_before", "winner"}
    if forbidden.intersection(public):
        raise ValueError("public feature encoder received private/match/outcome data")
    features: list[float] = []
    street_names = ("preflop", "flop", "turn", "river")
    street = public["street"]
    features.extend(float(street == name) for name in street_names)
    actor = public["actor"]
    features.extend(float(actor == value) for value in (0, 1, None))
    small_blind = int(public["small_blind"])
    features.extend(float(small_blind == player) for player in (0, 1))
    for field in ("stacks", "total_contributions", "street_bets"):
        values = public[field]
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError(f"public {field} is invalid")
        features.extend(float(value) / 20000.0 for value in values)
    counts = public["action_counts"]
    if not isinstance(counts, list) or len(counts) != 2:
        raise ValueError("public action counts are invalid")
    features.extend(float(value) / 20.0 for value in counts)
    contributions = public["total_contributions"]
    assert isinstance(contributions, list)
    features.append(sum(float(value) for value in contributions) / 40000.0)
    features.extend(
        float(bool(public[field]))
        for field in ("allin_occurred", "chance_pending", "runout_pending")
    )
    board = public["board"]
    if not isinstance(board, list):
        raise ValueError("public board is invalid")
    board_set = set(int(card) for card in board)
    features.extend(float(card in board_set) for card in range(52))
    rank_counts = [0] * 13
    suit_counts = [0] * 4
    for card in board_set:
        rank_counts[card // 4] += 1
        suit_counts[card % 4] += 1
    features.extend(count / 4.0 for count in rank_counts)
    features.extend(count / 5.0 for count in suit_counts)

    history = public["hand_history"]
    if not isinstance(history, list):
        raise ValueError("public action history is invalid")
    encoded_actions: list[list[float]] = []
    kinds = ("fold", "check", "call", "raise", "allin")
    for record in history[-MAX_HISTORY_ACTIONS:]:
        if not isinstance(record, dict):
            raise ValueError("public action record is invalid")
        row = [float(record["actor"] == player) for player in (0, 1)]
        row.extend(float(record["kind"] == kind) for kind in kinds)
        amount = record["amount"]
        row.append(0.0 if amount is None else float(amount) / 20000.0)
        row.extend(float(record["street"] == name) for name in street_names)
        encoded_actions.append(row)
    padding = [[0.0] * 12 for _ in range(MAX_HISTORY_ACTIONS - len(encoded_actions))]
    for row in padding + encoded_actions:
        features.extend(row)
    result = np.asarray(features, dtype=np.float32)
    if result.shape != (PUBLIC_FEATURE_DIM,) or not np.all(np.isfinite(result)):
        raise RuntimeError(
            f"public feature schema drifted: expected {PUBLIC_FEATURE_DIM}, got {result.shape}"
        )
    return result


def public_family_payload(state: NationalGameState) -> dict[str, object]:
    state.assert_invariants()
    if state.is_terminal:
        raise ValueError("terminal/outcome state cannot define a public input family")
    public = state.hand_public_dict()
    public.pop("terminal_reason")
    public.pop("winner")
    board = public.pop("board")
    return {
        "schema": "route-a1-m5b-public-family-suit-isomorphic-v1",
        "public_state_without_board": public,
        "board_suit_isomorphic_family": [
            list(card) for card in canonical_board_family(board)
        ],
    }


def public_family_id(state: NationalGameState) -> str:
    return _digest(public_family_payload(state))


@dataclass(frozen=True, slots=True)
class PreLabelPlan:
    """Typed, outcome-free identity used before any solve/label is run."""

    sample_id: str
    pbs_state_id: str
    public_family_id: str
    trajectory_id: str
    rollout_group_id: str
    augmentation_parent_sample_id: str | None
    source_copy_group_id: str
    source_checkpoint_digest: str
    decision_index: int
    seed_group: int

    def __post_init__(self) -> None:
        for field in (
            "sample_id",
            "pbs_state_id",
            "public_family_id",
            "trajectory_id",
            "rollout_group_id",
            "source_copy_group_id",
            "source_checkpoint_digest",
        ):
            if not _is_digest(getattr(self, field)):
                raise ValueError(f"pre-label {field} must be a content digest")
        if self.augmentation_parent_sample_id is not None and not _is_digest(
            self.augmentation_parent_sample_id
        ):
            raise ValueError("augmentation parent must be a sample digest")
        if type(self.decision_index) is not int or self.decision_index < 0:
            raise ValueError("decision index must be non-negative")
        if type(self.seed_group) is not int or self.seed_group < 0:
            raise ValueError("seed group must be non-negative")
        expected = _digest(
            {
                "schema": "route-a1-m5b-prelabel-sample-id-v1",
                "route_domain_salt": ROUTE_DOMAIN_SALT,
                "pbs_state_id": self.pbs_state_id,
                "public_family_id": self.public_family_id,
                "trajectory_id": self.trajectory_id,
                "rollout_group_id": self.rollout_group_id,
                "source_copy_group_id": self.source_copy_group_id,
                "source_checkpoint_digest": self.source_checkpoint_digest,
                "decision_index": self.decision_index,
                "seed_group": self.seed_group,
            }
        )
        if self.sample_id != expected:
            raise ValueError("pre-label sample ID is not content-derived")

    def payload(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "pbs_state_id": self.pbs_state_id,
            "public_family_id": self.public_family_id,
            "trajectory_id": self.trajectory_id,
            "rollout_group_id": self.rollout_group_id,
            "augmentation_parent_sample_id": self.augmentation_parent_sample_id,
            "source_copy_group_id": self.source_copy_group_id,
            # Checkpoint is metadata; it is intentionally not a global union
            # edge, otherwise a complete generation round becomes one cluster.
            "source_checkpoint_digest": self.source_checkpoint_digest,
            "decision_index": self.decision_index,
            "seed_group": self.seed_group,
            "labels_generated": False,
            "outcome_present": False,
        }


def make_prelabel_plan(
    state: NationalGameState,
    pbs: HUNLReachFactorPublicBeliefState,
    *,
    trajectory_id: str,
    rollout_group_id: str,
    source_copy_group_id: str,
    source_checkpoint_digest: str,
    decision_index: int,
    seed_group: int,
    augmentation_parent_sample_id: str | None = None,
) -> PreLabelPlan:
    pbs.assert_matches(state)
    family = public_family_id(state)
    payload = {
        "schema": "route-a1-m5b-prelabel-sample-id-v1",
        "route_domain_salt": ROUTE_DOMAIN_SALT,
        "pbs_state_id": pbs.pbs_state_id,
        "public_family_id": family,
        "trajectory_id": trajectory_id,
        "rollout_group_id": rollout_group_id,
        "source_copy_group_id": source_copy_group_id,
        "source_checkpoint_digest": source_checkpoint_digest,
        "decision_index": decision_index,
        "seed_group": seed_group,
    }
    return PreLabelPlan(
        sample_id=_digest(payload),
        pbs_state_id=pbs.pbs_state_id,
        public_family_id=family,
        trajectory_id=trajectory_id,
        rollout_group_id=rollout_group_id,
        augmentation_parent_sample_id=augmentation_parent_sample_id,
        source_copy_group_id=source_copy_group_id,
        source_checkpoint_digest=source_checkpoint_digest,
        decision_index=decision_index,
        seed_group=seed_group,
    )


class _UnionFind:
    def __init__(self, keys: Iterable[str]):
        self.parent = {key: key for key in keys}

    def find(self, key: str) -> str:
        parent = self.parent[key]
        if parent != key:
            self.parent[key] = self.find(parent)
        return self.parent[key]

    def union(self, first: str, second: str) -> None:
        left, right = self.find(first), self.find(second)
        if left == right:
            return
        if left < right:
            self.parent[right] = left
        else:
            self.parent[left] = right


def prelabel_plan_digest(plans: Sequence[PreLabelPlan]) -> str:
    return _digest(
        {
            "schema": "route-a1-m5b-external-prelabel-plan-v1",
            "route_domain_salt": ROUTE_DOMAIN_SALT,
            "records": [plan.payload() for plan in sorted(plans, key=lambda item: item.sample_id)],
        }
    )


def build_split_manifest(
    plans: Sequence[PreLabelPlan],
    *,
    expected_prelabel_plan_digest: str,
    split_seed: int,
    basis_points: Mapping[str, int],
    minimum_components: Mapping[str, int] | None = None,
    previous_manifest: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not plans or len({plan.sample_id for plan in plans}) != len(plans):
        raise ValueError("pre-label plans must be nonempty and unique")
    actual_plan_digest = prelabel_plan_digest(plans)
    if actual_plan_digest != expected_prelabel_plan_digest:
        raise ValueError("pre-label record closure differs from external commitment")
    if set(basis_points) != set(SPLITS) or any(
        type(value) is not int or value <= 0 for value in basis_points.values()
    ) or sum(basis_points.values()) != 10_000:
        raise ValueError("split basis points are invalid")
    if type(split_seed) is not int or split_seed < 0:
        raise ValueError("split seed must be a non-negative frozen integer")
    by_id = {plan.sample_id: plan for plan in plans}
    union = _UnionFind(by_id)
    edge_receipts: list[dict[str, str]] = []

    def connect(namespace: str, groups: Mapping[object, list[str]]) -> None:
        for token, members in sorted(groups.items(), key=lambda item: str(item[0])):
            ordered = sorted(members)
            for member in ordered[1:]:
                union.union(ordered[0], member)
                edge_receipts.append(
                    {
                        "namespace": namespace,
                        "token_sha256": _digest({"namespace": namespace, "token": token}),
                        "first": ordered[0],
                        "second": member,
                    }
                )

    for namespace, field in (
        ("same_canonical_public_family", "public_family_id"),
        ("same_trajectory", "trajectory_id"),
        ("same_rollout_group", "rollout_group_id"),
        ("same_source_sample_checkpoint_identity", "source_copy_group_id"),
        ("duplicate_mathematical_pbs", "pbs_state_id"),
    ):
        grouped: dict[object, list[str]] = {}
        for plan in plans:
            grouped.setdefault(getattr(plan, field), []).append(plan.sample_id)
        connect(namespace, grouped)
    for plan in plans:
        parent = plan.augmentation_parent_sample_id
        if parent is not None:
            if parent not in by_id:
                raise ValueError("augmentation parent is outside committed record closure")
            union.union(plan.sample_id, parent)
            edge_receipts.append(
                {
                    "namespace": "same_augmentation_parent",
                    "token_sha256": _digest({"parent": parent}),
                    "first": min(plan.sample_id, parent),
                    "second": max(plan.sample_id, parent),
                }
            )

    components: dict[str, list[str]] = {}
    for sample_id in sorted(by_id):
        components.setdefault(union.find(sample_id), []).append(sample_id)
    split_records: dict[str, str] = {}
    component_rows: list[dict[str, object]] = []
    train_end = int(basis_points["train"])
    validation_end = train_end + int(basis_points["validation"])
    family_base_splits: dict[str, str] = {}
    for family in sorted({plan.public_family_id for plan in plans}):
        bucket_digest = _digest(
            {
                "route_domain_salt": ROUTE_DOMAIN_SALT,
                "public_family_id": family,
                "split_seed": split_seed,
            }
        )
        bucket = int(bucket_digest[:16], 16) % 10_000
        family_base_splits[family] = (
            "train"
            if bucket < train_end
            else "validation"
            if bucket < validation_end
            else "test"
        )
    conservative_priority = {"train": 0, "validation": 1, "test": 2}
    for members in sorted(components.values()):
        component_digest = _digest(
            {
                "route_domain_salt": ROUTE_DOMAIN_SALT,
                "members": members,
                "public_families": sorted(
                    {by_id[member].public_family_id for member in members}
                ),
                "split_seed": split_seed,
            }
        )
        member_families = sorted(
            {by_id[member].public_family_id for member in members}
        )
        split = max(
            (family_base_splits[family] for family in member_families),
            key=conservative_priority.__getitem__,
        )
        for member in members:
            split_records[member] = split
        component_rows.append(
            {
                "component_sha256": component_digest,
                "member_sample_ids": members,
                "public_family_ids": member_families,
                "public_family_base_splits": {
                    family: family_base_splits[family] for family in member_families
                },
                "split": split,
            }
        )
    component_counts = {
        split: sum(row["split"] == split for row in component_rows)
        for split in SPLITS
    }
    if minimum_components is not None:
        if set(minimum_components) != set(SPLITS):
            raise ValueError("minimum split component contract differs")
        for split in SPLITS:
            if component_counts[split] < int(minimum_components[split]):
                raise ValueError(
                    f"split {split} has {component_counts[split]} components, "
                    f"requires {minimum_components[split]}"
                )
    split_component_digests = {
        split: _digest(
            sorted(
                row["component_sha256"]
                for row in component_rows
                if row["split"] == split
            )
        )
        for split in SPLITS
    }
    if len(set(split_component_digests.values())) != 3:
        raise ValueError("three split component closures are not distinct")
    if previous_manifest is not None:
        previous_records = previous_manifest.get("sample_splits")
        previous_families = previous_manifest.get("family_base_splits")
        if not isinstance(previous_records, dict) or not isinstance(previous_families, dict):
            raise ValueError("previous split manifest lacks immutable assignments")
        if not set(previous_records).issubset(split_records):
            raise ValueError("monotonic split extension removed prior samples")
        for sample_id, old_split in previous_records.items():
            if split_records[sample_id] != old_split:
                raise ValueError(
                    "split extension bridge would migrate an existing sample"
                )
        for family, old_split in previous_families.items():
            if family_base_splits.get(family) != old_split:
                raise ValueError(
                    "split extension would migrate an existing public family"
                )
    manifest = {
        "schema": M5B_SPLIT_SCHEMA,
        "route_domain_salt": ROUTE_DOMAIN_SALT,
        "split_seed": split_seed,
        "basis_points": dict(basis_points),
        "prelabel_plan_sha256": actual_plan_digest,
        "prelabel_record_count": len(plans),
        "edge_namespaces": list(EDGE_NAMESPACES),
        "edge_receipts": sorted(
            edge_receipts,
            key=lambda row: (row["namespace"], row["first"], row["second"]),
        ),
        "components": component_rows,
        "component_counts": component_counts,
        "family_base_splits": family_base_splits,
        "split_component_digests": split_component_digests,
        "sample_splits": split_records,
        "labels_generated_at_split_time": False,
        "outcomes_used_for_split": False,
        "source_checkpoint_digest_is_metadata_not_union_edge": True,
        "component_assignment_policy": "stable_family_bucket_then_test_validation_train_conservative_v1",
        "monotonic_extension_checked": previous_manifest is not None,
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest


def verify_split_manifest(
    manifest: Mapping[str, object],
    plans: Sequence[PreLabelPlan],
    *,
    expected_prelabel_plan_digest: str,
    split_seed: int,
    basis_points: Mapping[str, int],
    minimum_components: Mapping[str, int] | None = None,
    previous_manifest: Mapping[str, object] | None = None,
) -> dict[str, object]:
    rebuilt = build_split_manifest(
        plans,
        expected_prelabel_plan_digest=expected_prelabel_plan_digest,
        split_seed=split_seed,
        basis_points=basis_points,
        minimum_components=minimum_components,
        previous_manifest=previous_manifest,
    )
    if dict(manifest) != rebuilt:
        raise ValueError("split manifest differs from external pre-label closure")
    return rebuilt


def _no_clobber_write(path: Path, data: bytes) -> None:
    path = Path(path)
    _reject_symlink_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(path.parent)
    parent_stat = path.parent.lstat()
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise ValueError(f"publication directory is a symlink or not a directory: {path.parent}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _stable_regular_bytes(path, label="published artifact") != data:
                raise ValueError(f"content-addressed path collision: {path}")
        directory = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@dataclass(frozen=True, slots=True)
class TestOnceSeal:
    model_sha256: str
    threshold_sha256: str
    strongest_baseline_sha256: str
    split_manifest_sha256: str

    def __post_init__(self) -> None:
        for field in (
            "model_sha256",
            "threshold_sha256",
            "strongest_baseline_sha256",
            "split_manifest_sha256",
        ):
            if not _is_digest(getattr(self, field)):
                raise ValueError(f"test-once {field} is invalid")

    def payload(self) -> dict[str, object]:
        return {
            "schema": "route-a1-m5b-test-once-seal-v1",
            "model_sha256": self.model_sha256,
            "threshold_sha256": self.threshold_sha256,
            "strongest_baseline_sha256": self.strongest_baseline_sha256,
            "split_manifest_sha256": self.split_manifest_sha256,
            "state": "frozen_unconsumed",
        }


def write_test_once_seal(path: Path, seal: TestOnceSeal) -> None:
    _no_clobber_write(
        path, json.dumps(seal.payload(), sort_keys=True).encode("utf-8") + b"\n"
    )


def canonical_test_once_receipt_path(seal_path: Path, seal: TestOnceSeal) -> Path:
    expected_bytes = json.dumps(seal.payload(), sort_keys=True).encode("utf-8") + b"\n"
    seal_sha256 = sha256_bytes(expected_bytes)
    return (
        Path(seal_path).parent
        / ".m5b-test-once-consumptions"
        / f"{seal_sha256}.json"
    )


def consume_test_once_seal(
    seal_path: Path,
    seal: TestOnceSeal,
) -> dict[str, object]:
    """Atomically consume a durable frozen test authorization exactly once."""

    expected_bytes = json.dumps(seal.payload(), sort_keys=True).encode("utf-8") + b"\n"
    try:
        observed_seal = _stable_regular_bytes(seal_path, label="test-once seal")
    except ValueError as exc:
        raise ValueError("test-once frozen seal is absent or drifted") from exc
    if observed_seal != expected_bytes:
        raise ValueError("test-once frozen seal is absent or drifted")
    receipt_path = canonical_test_once_receipt_path(seal_path, seal)
    receipt = {
        "schema": "route-a1-m5b-test-once-consumption-v1",
        "seal_sha256": sha256_bytes(expected_bytes),
        "model_sha256": seal.model_sha256,
        "threshold_sha256": seal.threshold_sha256,
        "strongest_baseline_sha256": seal.strongest_baseline_sha256,
        "split_manifest_sha256": seal.split_manifest_sha256,
        "state": "consumed",
    }
    data = json.dumps(receipt, sort_keys=True).encode("utf-8") + b"\n"
    _reject_symlink_components(receipt_path.parent)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(receipt_path.parent)
    parent_stat = receipt_path.parent.lstat()
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise ValueError("test-once receipt directory is not authoritative")
    try:
        descriptor = os.open(
            receipt_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError as exc:
        raise ValueError("test split has already been consumed") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(
            receipt_path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        # A present receipt is fail-closed even if the writer crashed after
        # exclusive creation; never unlink it and permit a second test open.
        raise
    return receipt


def sample_arrays(
    pbs: HUNLReachFactorPublicBeliefState,
    actions: AbstractActionSet,
    policy_target: np.ndarray,
    targets: PrivateTargets,
) -> dict[str, np.ndarray]:
    policy = np.asarray(policy_target, dtype=np.float32)
    if policy.shape != (HUNL_COMBO_COUNT, ACTION_COUNT):
        raise ValueError("actor policy target must have shape [1326,9]")
    if not np.any(actions.mask):
        raise ValueError("actor policy target has no legal action support")
    legal_combos = np.asarray(pbs.legal_mask(), dtype=bool)
    if (
        not np.all(np.isfinite(policy))
        or np.any(policy < 0.0)
        or np.any(policy[:, ~actions.mask] != 0.0)
        or not np.allclose(
            policy[legal_combos].sum(axis=1), 1.0, atol=1e-6, rtol=0.0
        )
    ):
        raise ValueError("actor policy target violates exact legal support")
    actor = pbs.public_state.get("actor")
    if type(actor) is not int or actor not in (0, 1):
        raise ValueError("policy target requires one exact public actor")
    return {
        "public_features": encode_public_features(pbs.public_state),
        "actor": np.asarray(actor, dtype=np.int8),
        "reach_factors": np.asarray(pbs.reach_factors, dtype=np.float32),
        "legal_combo_mask": legal_combos.astype(np.bool_),
        "legal_action_mask": actions.mask.astype(np.bool_),
        "oracle_on_policy_private_values": targets.normalized_values.astype(np.float64),
        "oracle_value_valid_mask": targets.value_valid_mask.astype(np.bool_),
        "projected_marginals": targets.projected_marginals.astype(np.float64),
        "oracle_actor_policy": policy,
        "diagnostic_unnormalized_cfvs": targets.unnormalized_cfvs.astype(np.float32),
        "diagnostic_conditional_q": targets.actor_normalized_q.astype(np.float32),
        "diagnostic_q_valid_mask": targets.actor_q_valid_mask.astype(np.bool_),
    }


def write_sample_shard(
    directory: Path,
    *,
    plan: PreLabelPlan,
    split_manifest: Mapping[str, object],
    pbs: HUNLReachFactorPublicBeliefState,
    actions: AbstractActionSet,
    policy_target: np.ndarray,
    targets: PrivateTargets,
    generator_config_sha256: str,
    split_authority_sha256: str,
    split_authority_source_closure_sha256: str,
    search_result_sha256: str,
    solver_seed: int,
    target_seed: int,
) -> dict[str, object]:
    sample_splits = split_manifest.get("sample_splits")
    if not isinstance(sample_splits, dict) or plan.sample_id not in sample_splits:
        raise ValueError("sample has no pre-label split assignment")
    if not _is_digest(split_manifest.get("manifest_sha256")):
        raise ValueError("sample publication requires a frozen split manifest digest")
    split_manifest_sha256 = split_manifest.get("manifest_sha256")
    arrays = sample_arrays(pbs, actions, policy_target, targets)
    _reject_symlink_components(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(directory)
    directory_stat = directory.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
        raise ValueError("sample publication directory is a symlink or not a directory")
    final = directory / f"{plan.sample_id}.npz"
    # np.savez writes deterministic member order but ZIP timestamps are not a
    # stable identity; the raw file digest is recorded, while array content gets
    # its own canonical digest.
    array_digest = hashlib.sha256()
    for name in sorted(arrays):
        array = np.ascontiguousarray(arrays[name])
        array_digest.update(name.encode("utf-8") + b"\0")
        array_digest.update(array.dtype.str.encode("ascii") + b"\0")
        array_digest.update(canonical_bytes(list(array.shape)))
        array_digest.update(array.tobytes())
    array_content_sha256 = array_digest.hexdigest()
    weighted_zero_sum_residual_chips = math.fsum(
        float(arrays["projected_marginals"][player, hand])
        * float(arrays["oracle_on_policy_private_values"][player, hand])
        for player in (0, 1)
        for hand in range(HUNL_COMBO_COUNT)
    )
    generator_receipt = _build_generator_receipt(
        plan=plan,
        pbs_network_input_sha256=pbs.network_input_sha256,
        split_manifest_sha256=str(split_manifest_sha256),
        split_authority_sha256=split_authority_sha256,
        split_authority_source_closure_sha256=(
            split_authority_source_closure_sha256
        ),
        generator_config_sha256=generator_config_sha256,
        search_result_sha256=search_result_sha256,
        solver_seed=solver_seed,
        target_seed=target_seed,
        array_content_sha256=array_content_sha256,
        weighted_zero_sum_residual_chips=weighted_zero_sum_residual_chips,
    )
    metadata_path = directory / f"{plan.sample_id}.json"
    final_exists = os.path.lexists(final)
    metadata_exists = os.path.lexists(metadata_path)
    if final_exists or metadata_exists:
        if not (final_exists and metadata_exists):
            raise ValueError("partial content-addressed sample publication exists")
        metadata_raw = _stable_regular_bytes(
            metadata_path, label="existing sample metadata"
        )
        existing = json.loads(metadata_raw)
        if metadata_raw != (
            json.dumps(existing, sort_keys=True, allow_nan=False).encode("utf-8")
            + b"\n"
        ):
            raise ValueError("existing sample metadata is not canonical")
        if existing.get("array_content_sha256") != array_content_sha256:
            raise ValueError("sample ID collides with different array content")
        npz_raw = _stable_regular_bytes(final, label="existing sample NPZ")
        if hashlib.sha256(npz_raw).hexdigest() != existing.get("npz_raw_sha256"):
            raise ValueError("existing sample NPZ digest drifted")
        with np.load(io.BytesIO(npz_raw), allow_pickle=False) as payload:
            if payload.files != list(arrays) or any(
                not np.array_equal(payload[name], arrays[name]) for name in arrays
            ):
                raise ValueError("existing sample arrays differ at the same ID")
        if existing.get("generator_receipt") != generator_receipt:
            raise ValueError("sample ID collides with different generator provenance")
        return existing

    descriptor, temporary_text = tempfile.mkstemp(
        prefix=f".{plan.sample_id}.", suffix=".npz.tmp", dir=directory
    )
    temporary = Path(temporary_text)
    with os.fdopen(descriptor, "wb") as handle:
        np.savez_compressed(handle, **arrays)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, final)
    except FileExistsError:
        npz_raw = _stable_regular_bytes(final, label="concurrent sample NPZ")
        with np.load(io.BytesIO(npz_raw), allow_pickle=False) as payload:
            if payload.files != list(arrays) or any(
                not np.array_equal(payload[name], arrays[name]) for name in arrays
            ):
                raise ValueError("concurrent sample publication collision")
    finally:
        temporary.unlink(missing_ok=True)
    _reject_symlink_components(directory)
    directory_fd = os.open(
        directory,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    npz_raw = _stable_regular_bytes(final, label="published sample NPZ")
    metadata = {
        "schema": M5B_SAMPLE_SCHEMA,
        "sample_id": plan.sample_id,
        "split": sample_splits[plan.sample_id],
        "prelabel_plan": plan.payload(),
        "pbs_network_input_sha256": pbs.network_input_sha256,
        "pbs_network_input": pbs.network_input(),
        "pbs_public_state": pbs.public_state,
        "public_family_id": plan.public_family_id,
        "split_manifest_sha256": split_manifest_sha256,
        "action_support": actions.snapshot(),
        "primary_value_target": "oracle_on_policy_private_values",
        "primary_policy_target": "oracle_actor_policy",
        "q_cfv_training_authority": False,
        "payoff_unit": "chips",
        "payoff_origin": "net_from_initial_20000_chip_stack_v1",
        "array_content_sha256": array_content_sha256,
        "npz_raw_sha256": hashlib.sha256(npz_raw).hexdigest(),
        "generator_receipt": generator_receipt,
    }
    metadata["metadata_sha256"] = _digest(metadata)
    _no_clobber_write(
        metadata_path,
        json.dumps(metadata, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n",
    )
    return metadata
