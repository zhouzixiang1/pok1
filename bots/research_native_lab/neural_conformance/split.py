"""Externally pinned, monotonic leakage closure for neural datasets.

This module is a deterministic verifier, not a source of authority.  A route
must freeze and pin a :class:`SplitAuthority` before labels exist.  Verification
always receives that authority from outside the candidate manifest; rebuilding
a self-consistent manifest with edited records is therefore insufficient.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .prelabel import (
    PreLabelGeneratorJournal,
    ROUTE_DOMAINS,
    journal_artifact_registry_sha256,
    route_owned_content_inventory,
    semantic_artifact_inventory,
)
from .public_family import public_family_payload_id, validate_public_family_payload


LEAKAGE_SPLIT_SCHEMA = "neural-leakage-closed-public-family-split-v1"
SPLIT_AUTHORITY_SCHEMA = "neural-prelabel-split-authority-v1"
PRELABEL_FREEZE_SCHEMA = "neural-prelabel-freeze-content-v1"
PUBLIC_FAMILY_REGISTRY_SCHEMA = "neural-public-family-registry-v1"
_SPLITS = ("train", "validation", "test")
_SPLIT_PRIORITY = {"train": 0, "validation": 1, "test": 2}
_EDGE_FIELDS = (
    "public_family_id",
    "pbs_group_id",
    "trajectory_group_id",
    "rollout_group_id",
    "source_copy_group_id",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _route_salt(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(character.isspace() or ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("route_salt must be bounded printable ASCII without whitespace")
    return value


def content_id(namespace: str, payload: object) -> str:
    """Create a domain-separated provenance ID from canonical pre-label data."""

    if (
        type(namespace) is not str
        or not namespace
        or len(namespace) > 128
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_:."
            for character in namespace
        )
    ):
        raise ValueError("content-ID namespace is not canonical")
    return _digest({"namespace": namespace, "payload": payload})


@dataclass(frozen=True, slots=True)
class LeakageRecord:
    """Journal-derived sample identity and complete typed relation closure.

    Callers do not construct records for a formal split.  They are replayed
    from :class:`PreLabelGeneratorJournal`.  ``source_checkpoint_id`` remains
    provenance metadata rather than a union edge so one generator checkpoint
    does not collapse an entire generation round into one component.
    """

    sample_id: str
    public_family_id: str
    source_checkpoint_id: str
    pbs_group_id: str
    trajectory_group_id: str
    rollout_group_id: str
    source_copy_group_id: str
    augmentation_parent_sample_id: str | None

    def validated(self) -> "LeakageRecord":
        _sha256(self.sample_id, "sample_id")
        _sha256(self.public_family_id, "public_family_id")
        _sha256(self.source_checkpoint_id, "source_checkpoint_id")
        for field in _EDGE_FIELDS[1:]:
            _sha256(getattr(self, field), field)
        _sha256(
            self.augmentation_parent_sample_id,
            "augmentation_parent_sample_id",
            optional=True,
        )
        return self


def records_from_journal(
    journal: PreLabelGeneratorJournal,
    artifact_root: Path,
    public_family_registry: Mapping[str, Mapping[str, Any]],
) -> list[LeakageRecord]:
    """Replay the only accepted relation records from typed, hashed events."""

    journal.validated(artifact_root, public_family_registry)
    artifacts = journal.artifact_map()
    records: list[LeakageRecord] = []
    for event in journal.events:
        records.append(
            LeakageRecord(
                sample_id=event.sample_id,
                public_family_id=event.public_family_id,
                source_checkpoint_id=artifacts[
                    event.source_checkpoint_artifact_id
                ].semantic_id,
                pbs_group_id=artifacts[event.pbs_artifact_id].semantic_id,
                trajectory_group_id=artifacts[
                    event.trajectory_artifact_id
                ].semantic_id,
                rollout_group_id=artifacts[event.rollout_artifact_id].semantic_id,
                source_copy_group_id=artifacts[
                    event.source_copy_artifact_id
                ].semantic_id,
                augmentation_parent_sample_id=event.augmentation_parent_sample_id,
            ).validated()
        )
    sample_ids = {record.sample_id for record in records}
    for record in records:
        parent = record.augmentation_parent_sample_id
        if parent is not None and parent not in sample_ids:
            raise ValueError("journal-derived augmentation parent is outside record closure")
    return _normalized_records(records)


def _normalized_records(records: Iterable[LeakageRecord]) -> list[LeakageRecord]:
    normalized: list[LeakageRecord] = []
    for record in records:
        if type(record) is not LeakageRecord:
            raise TypeError("split records must be exact LeakageRecord instances")
        normalized.append(record.validated())
    normalized.sort(key=lambda row: row.sample_id)
    if not normalized:
        raise ValueError("leakage split requires at least one record")
    sample_ids = [record.sample_id for record in normalized]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("sample_id values must be unique")
    return normalized


def record_set_sha256(records: Iterable[LeakageRecord]) -> str:
    normalized = _normalized_records(records)
    return _digest([asdict(record) for record in normalized])


def provenance_graph_sha256(records: Iterable[LeakageRecord]) -> str:
    normalized = _normalized_records(records)
    return _digest(
        {
            "edge_fields": list(_EDGE_FIELDS),
            "edges": [
                {
                    "sample_id": record.sample_id,
                    **{field: getattr(record, field) for field in _EDGE_FIELDS},
                    "augmentation_parent_sample_id": (
                        record.augmentation_parent_sample_id
                    ),
                }
                for record in normalized
            ],
            "schema": "neural-prelabel-provenance-graph-v1",
        }
    )


def _normalized_public_family_registry(
    registry: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if type(registry) is not dict or not registry:
        raise ValueError("public-family registry must be a nonempty exact mapping")
    normalized: dict[str, dict[str, Any]] = {}
    for family_id, payload in registry.items():
        _sha256(family_id, "public-family registry key")
        validated = validate_public_family_payload(payload)
        if public_family_payload_id(validated) != family_id:
            raise ValueError("public-family registry key differs from Common payload")
        normalized[family_id] = validated
    return {key: normalized[key] for key in sorted(normalized)}


def public_family_registry_sha256(
    registry: Mapping[str, Mapping[str, Any]],
) -> str:
    normalized = _normalized_public_family_registry(registry)
    return _digest(
        {"families": normalized, "schema": PUBLIC_FAMILY_REGISTRY_SCHEMA}
    )


def _prelabel_freeze_digest(payload: Mapping[str, Any]) -> str:
    return _digest({"authority": dict(payload), "schema": PRELABEL_FREEZE_SCHEMA})


@dataclass(frozen=True, slots=True)
class SplitAuthority:
    """Route-owned facts that must be pinned outside the generated manifest."""

    route_domain: str
    route_contract_sha256: str
    prelabel_manifest_sha256: str
    generator_journal_sha256: str
    artifact_registry_sha256: str
    provenance_graph_sha256: str
    public_family_registry_sha256: str
    record_set_sha256: str
    route_salt_sha256: str
    train_basis_points: int
    validation_basis_points: int
    test_basis_points: int
    minimum_samples_per_split: int
    minimum_components_per_split: int
    minimum_families_per_split: int
    labels_absent_at_freeze: bool = True
    schema: str = SPLIT_AUTHORITY_SCHEMA

    def validated(self) -> "SplitAuthority":
        if self.schema != SPLIT_AUTHORITY_SCHEMA:
            raise ValueError("split authority schema differs")
        if self.route_domain not in ROUTE_DOMAINS:
            raise ValueError("split authority route is not registered")
        for field in (
            "route_contract_sha256",
            "prelabel_manifest_sha256",
            "generator_journal_sha256",
            "artifact_registry_sha256",
            "provenance_graph_sha256",
            "public_family_registry_sha256",
            "record_set_sha256",
            "route_salt_sha256",
        ):
            _sha256(getattr(self, field), field)
        basis = self.basis_points
        if (
            any(type(value) is not int for value in basis.values())
            or sum(basis.values()) != 10_000
            or any(value <= 0 for value in basis.values())
        ):
            raise ValueError("authority split basis points must be positive and sum to 10,000")
        for field in (
            "minimum_samples_per_split",
            "minimum_components_per_split",
            "minimum_families_per_split",
        ):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field} must be a positive exact integer")
        if self.labels_absent_at_freeze is not True:
            raise ValueError("split authority must be frozen before labels exist")
        commitment = asdict(self)
        supplied = commitment.pop("prelabel_manifest_sha256")
        if supplied != _prelabel_freeze_digest(commitment):
            raise ValueError("pre-label freeze digest differs from authority content")
        return self

    @property
    def basis_points(self) -> dict[str, int]:
        return {
            "train": self.train_basis_points,
            "validation": self.validation_basis_points,
            "test": self.test_basis_points,
        }

    def to_payload(self) -> dict[str, Any]:
        self.validated()
        return asdict(self)

    @property
    def digest(self) -> str:
        return _digest(self.to_payload())


def freeze_split_authority(
    journal: PreLabelGeneratorJournal,
    artifact_root: Path,
    public_family_registry: Mapping[str, Mapping[str, Any]],
    *,
    route_domain: str,
    route_contract_sha256: str,
    route_salt: str,
    train_basis_points: int,
    validation_basis_points: int,
    test_basis_points: int,
    minimum_samples_per_split: int,
    minimum_components_per_split: int,
    minimum_families_per_split: int,
) -> SplitAuthority:
    """Create the content commitment that a route must pin before labeling."""

    if type(journal) is not PreLabelGeneratorJournal:
        raise TypeError("split freeze requires the exact typed generator journal")
    journal.validated(artifact_root, public_family_registry)
    if journal.route_domain != route_domain:
        raise ValueError("split route differs from generator journal route")
    contract_artifact = next(
        artifact for artifact in journal.artifacts if artifact.kind == "route-contract"
    )
    contract_facts = contract_artifact.verify_file(artifact_root)
    route_contract_sha256 = _sha256(
        route_contract_sha256, "route_contract_sha256"
    )
    if route_contract_sha256 != contract_facts["contract_sha256"]:
        raise ValueError("route contract digest differs from typed artifact evidence")
    normalized = records_from_journal(
        journal, artifact_root, public_family_registry
    )
    route_salt = _route_salt(route_salt)
    registry = _normalized_public_family_registry(public_family_registry)
    used_families = {record.public_family_id for record in normalized}
    if set(registry) != used_families:
        raise ValueError("public-family registry must exactly cover frozen records")
    base = {
        "route_domain": route_domain,
        "route_contract_sha256": route_contract_sha256,
        "generator_journal_sha256": journal.digest,
        "artifact_registry_sha256": journal_artifact_registry_sha256(journal),
        "provenance_graph_sha256": provenance_graph_sha256(normalized),
        "public_family_registry_sha256": public_family_registry_sha256(registry),
        "record_set_sha256": record_set_sha256(normalized),
        "route_salt_sha256": hashlib.sha256(route_salt.encode("ascii")).hexdigest(),
        "train_basis_points": train_basis_points,
        "validation_basis_points": validation_basis_points,
        "test_basis_points": test_basis_points,
        "minimum_samples_per_split": minimum_samples_per_split,
        "minimum_components_per_split": minimum_components_per_split,
        "minimum_families_per_split": minimum_families_per_split,
        "labels_absent_at_freeze": True,
        "schema": SPLIT_AUTHORITY_SCHEMA,
    }
    return SplitAuthority(
        prelabel_manifest_sha256=_prelabel_freeze_digest(base),
        **base,
    ).validated()


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        first, second = self.find(left), self.find(right)
        if first == second:
            return
        low, high = sorted((first, second))
        self.parent[high] = low


def _split_for_bucket(bucket: int, basis: Mapping[str, int]) -> str:
    if bucket < basis["train"]:
        return "train"
    if bucket < basis["train"] + basis["validation"]:
        return "validation"
    return "test"


def _family_assignment(
    public_family_id: str,
    *,
    route_domain: str,
    route_salt: str,
    basis: Mapping[str, int],
) -> dict[str, Any]:
    bucket_digest = hashlib.sha256(
        b"neural-public-family-split-v1\0"
        + route_domain.encode("ascii")
        + b"\0"
        + route_salt.encode("ascii")
        + b"\0"
        + public_family_id.encode("ascii")
    ).digest()
    bucket = int.from_bytes(bucket_digest[:8], "big") % 10_000
    return {
        "assignment_bucket": bucket,
        "public_family_id": public_family_id,
        "split": _split_for_bucket(bucket, basis),
    }


def _most_restrictive(splits: Iterable[str]) -> str:
    return max(splits, key=_SPLIT_PRIORITY.__getitem__)


def build_leakage_closed_split(
    journal: PreLabelGeneratorJournal,
    artifact_root: Path,
    public_family_registry: Mapping[str, Mapping[str, Any]],
    *,
    authority: SplitAuthority,
    route_salt: str,
) -> dict[str, Any]:
    """Union a frozen pre-label graph without allowing heldout downgrades."""

    authority = authority.validated()
    route_salt = _route_salt(route_salt)
    if hashlib.sha256(route_salt.encode("ascii")).hexdigest() != authority.route_salt_sha256:
        raise ValueError("route salt differs from externally pinned authority")
    if type(journal) is not PreLabelGeneratorJournal:
        raise TypeError("split build requires the exact typed generator journal")
    journal.validated(artifact_root, public_family_registry)
    if journal.route_domain != authority.route_domain:
        raise ValueError("generator journal route differs from split authority")
    if journal.digest != authority.generator_journal_sha256:
        raise ValueError("generator journal differs from externally pinned authority")
    if (
        journal_artifact_registry_sha256(journal)
        != authority.artifact_registry_sha256
    ):
        raise ValueError("artifact registry differs from externally pinned authority")
    normalized = records_from_journal(
        journal, artifact_root, public_family_registry
    )
    registry = _normalized_public_family_registry(public_family_registry)
    record_payload = [asdict(record) for record in normalized]
    observed_record_set = _digest(record_payload)
    if observed_record_set != authority.record_set_sha256:
        raise ValueError("record set differs from externally pinned pre-label authority")
    if provenance_graph_sha256(normalized) != authority.provenance_graph_sha256:
        raise ValueError("provenance graph differs from externally pinned authority")
    if public_family_registry_sha256(registry) != authority.public_family_registry_sha256:
        raise ValueError("public-family registry differs from externally pinned authority")
    if set(registry) != {record.public_family_id for record in normalized}:
        raise ValueError("public-family registry does not exactly cover split records")

    sample_ids = [record.sample_id for record in normalized]
    union = _UnionFind(sample_ids)
    owners: dict[tuple[str, str], str] = {}
    by_sample = {record.sample_id: record for record in normalized}
    for record in normalized:
        for field in _EDGE_FIELDS:
            value = getattr(record, field)
            if value is None:
                continue
            key = (field, value)
            previous = owners.setdefault(key, record.sample_id)
            union.union(previous, record.sample_id)
        parent = record.augmentation_parent_sample_id
        if parent is not None:
            union.union(parent, record.sample_id)

    members_by_root: dict[str, list[str]] = {}
    for sample_id in sample_ids:
        members_by_root.setdefault(union.find(sample_id), []).append(sample_id)
    if len(members_by_root) < len(_SPLITS):
        raise ValueError(
            "leakage closure leaves fewer than three components; "
            "train/validation/test cannot all be populated"
        )

    family_ids = sorted({record.public_family_id for record in normalized})
    family_assignments = {
        family_id: _family_assignment(
            family_id,
            route_domain=authority.route_domain,
            route_salt=route_salt,
            basis=authority.basis_points,
        )
        for family_id in family_ids
    }

    components: list[dict[str, Any]] = []
    sample_rows: list[dict[str, str]] = []
    split_components = {name: [] for name in _SPLITS}
    split_samples = {name: set() for name in _SPLITS}
    split_families = {name: set() for name in _SPLITS}
    for members in sorted((sorted(items) for items in members_by_root.values())):
        component_families = sorted({by_sample[item].public_family_id for item in members})
        component_split = _most_restrictive(
            family_assignments[family_id]["split"] for family_id in component_families
        )
        component_id = _digest(
            {
                "families": component_families,
                "members": members,
                "route_domain": authority.route_domain,
                "schema": LEAKAGE_SPLIT_SCHEMA,
            }
        )
        components.append(
            {
                "component_id": component_id,
                "public_family_ids": component_families,
                "members": members,
                "split": component_split,
            }
        )
        split_components[component_split].append(component_id)
        split_samples[component_split].update(members)
        split_families[component_split].update(component_families)
        sample_rows.extend(
            {
                "component_id": component_id,
                "public_family_id": by_sample[sample_id].public_family_id,
                "sample_id": sample_id,
                "split": component_split,
            }
            for sample_id in members
        )

    split_stats = {
        name: {
            "component_count": len(split_components[name]),
            "family_count": len(split_families[name]),
            "sample_count": len(split_samples[name]),
        }
        for name in _SPLITS
    }
    minima = {
        "component_count": authority.minimum_components_per_split,
        "family_count": authority.minimum_families_per_split,
        "sample_count": authority.minimum_samples_per_split,
    }
    for split, stats in split_stats.items():
        for field, minimum in minima.items():
            if stats[field] < minimum:
                raise ValueError(
                    f"{split} {field}={stats[field]} is below externally pinned minimum {minimum}"
                )

    payload: dict[str, Any] = {
        "authority": authority.to_payload(),
        "authority_sha256": authority.digest,
        "components": components,
        "edge_fields": list(_EDGE_FIELDS),
        "family_assignments": [family_assignments[item] for item in family_ids],
        "generator_journal": journal.to_payload(),
        "generator_journal_sha256": journal.digest,
        "record_set_sha256": observed_record_set,
        "records": record_payload,
        "samples": sorted(sample_rows, key=lambda row: row["sample_id"]),
        "schema": LEAKAGE_SPLIT_SCHEMA,
        "split_component_ids": {
            name: sorted(split_components[name]) for name in _SPLITS
        },
        "split_stats": split_stats,
    }
    payload["manifest_sha256"] = _digest(payload)
    return json.loads(_canonical_bytes(payload))


def _strict_manifest_fields(manifest: Mapping[str, Any]) -> None:
    required = {
        "authority",
        "authority_sha256",
        "components",
        "edge_fields",
        "family_assignments",
        "generator_journal",
        "generator_journal_sha256",
        "manifest_sha256",
        "record_set_sha256",
        "records",
        "samples",
        "schema",
        "split_component_ids",
        "split_stats",
    }
    if type(manifest) is not dict or set(manifest) != required:
        raise ValueError("split manifest fields differ")
    if manifest.get("schema") != LEAKAGE_SPLIT_SCHEMA:
        raise ValueError("split manifest schema differs")


def verify_leakage_closed_split(
    manifest: Mapping[str, Any],
    artifact_root: Path,
    public_family_registry: Mapping[str, Mapping[str, Any]],
    *,
    route_salt: str,
    authority: SplitAuthority,
    expected_authority_sha256: str,
) -> None:
    """Recompute against an externally supplied, independently pinned authority."""

    _strict_manifest_fields(manifest)
    authority = authority.validated()
    _sha256(expected_authority_sha256, "expected_authority_sha256")
    if authority.digest != expected_authority_sha256:
        raise ValueError("external split authority digest differs from pinned digest")
    if manifest["authority"] != authority.to_payload() or manifest[
        "authority_sha256"
    ] != expected_authority_sha256:
        raise ValueError("manifest does not embed the externally pinned authority")
    try:
        journal = PreLabelGeneratorJournal.from_payload(
            manifest["generator_journal"]
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("split manifest contains an invalid generator journal") from exc
    if manifest["generator_journal_sha256"] != journal.digest:
        raise ValueError("split manifest generator-journal digest differs")
    expected = build_leakage_closed_split(
        journal,
        artifact_root,
        public_family_registry,
        authority=authority,
        route_salt=route_salt,
    )
    if dict(manifest) != expected:
        raise ValueError("split manifest differs from authority-bound recomputation")


def verify_monotonic_extension(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    previous_artifact_root: Path,
    current_artifact_root: Path,
    previous_public_family_registry: Mapping[str, Mapping[str, Any]],
    current_public_family_registry: Mapping[str, Mapping[str, Any]],
    *,
    route_salt: str,
    previous_authority: SplitAuthority,
    current_authority: SplitAuthority,
    previous_authority_sha256: str,
    current_authority_sha256: str,
) -> None:
    """Prove a later round cannot move an already frozen sample across splits."""

    verify_leakage_closed_split(
        previous,
        previous_artifact_root,
        previous_public_family_registry,
        route_salt=route_salt,
        authority=previous_authority,
        expected_authority_sha256=previous_authority_sha256,
    )
    verify_leakage_closed_split(
        current,
        current_artifact_root,
        current_public_family_registry,
        route_salt=route_salt,
        authority=current_authority,
        expected_authority_sha256=current_authority_sha256,
    )
    stable_authority_fields = (
        "route_domain",
        "route_contract_sha256",
        "route_salt_sha256",
        "train_basis_points",
        "validation_basis_points",
        "test_basis_points",
    )
    for field in stable_authority_fields:
        if getattr(previous_authority, field) != getattr(current_authority, field):
            raise ValueError(f"split extension changed stable authority field {field}")
    for field in (
        "minimum_samples_per_split",
        "minimum_components_per_split",
        "minimum_families_per_split",
    ):
        if getattr(current_authority, field) < getattr(previous_authority, field):
            raise ValueError(f"split extension weakened authority field {field}")
    previous_registry = _normalized_public_family_registry(
        previous_public_family_registry
    )
    current_registry = _normalized_public_family_registry(current_public_family_registry)
    if not previous_registry.keys() <= current_registry.keys():
        raise ValueError("split extension removed a frozen public family")
    for family_id, payload in previous_registry.items():
        if current_registry[family_id] != payload:
            raise ValueError("split extension mutated a frozen public family")
    previous_journal = PreLabelGeneratorJournal.from_payload(
        previous["generator_journal"]
    )
    current_journal = PreLabelGeneratorJournal.from_payload(
        current["generator_journal"]
    )
    previous_artifacts = {
        row["instance_id"]: row
        for row in previous_journal.to_payload()["artifacts"]
    }
    current_artifacts = {
        row["instance_id"]: row
        for row in current_journal.to_payload()["artifacts"]
    }
    if not previous_artifacts.keys() <= current_artifacts.keys():
        raise ValueError("split extension removed a frozen pre-label artifact")
    for artifact_id, payload in previous_artifacts.items():
        if current_artifacts[artifact_id] != payload:
            raise ValueError("split extension mutated a frozen pre-label artifact")
    previous_events = {
        row["sample_id"]: row for row in previous_journal.to_payload()["events"]
    }
    current_events = {
        row["sample_id"]: row for row in current_journal.to_payload()["events"]
    }
    if not previous_events.keys() <= current_events.keys():
        raise ValueError("split extension removed a frozen generator event")
    for sample_id, payload in previous_events.items():
        if current_events[sample_id] != payload:
            raise ValueError("split extension mutated a frozen generator event")
    previous_family_assignments = {
        row["public_family_id"]: row for row in previous["family_assignments"]
    }
    current_family_assignments = {
        row["public_family_id"]: row for row in current["family_assignments"]
    }
    for family_id, assignment in previous_family_assignments.items():
        if current_family_assignments.get(family_id) != assignment:
            raise ValueError("split extension changed a frozen family assignment")
    previous_records = {row["sample_id"]: row for row in previous["records"]}
    current_records = {row["sample_id"]: row for row in current["records"]}
    if not previous_records.keys() <= current_records.keys():
        raise ValueError("split extension removed a previously frozen sample")
    for sample_id, record in previous_records.items():
        if current_records[sample_id] != record:
            raise ValueError("split extension mutated a previously frozen sample")
    previous_splits = {row["sample_id"]: row["split"] for row in previous["samples"]}
    current_splits = {row["sample_id"]: row["split"] for row in current["samples"]}
    for sample_id, split in previous_splits.items():
        if current_splits.get(sample_id) != split:
            raise ValueError(
                "split extension moved a previously frozen sample across splits"
            )


def verify_cross_route_independence(
    first_authority: SplitAuthority,
    second_authority: SplitAuthority,
    first_journal: PreLabelGeneratorJournal,
    second_journal: PreLabelGeneratorJournal,
    first_artifact_root: Path,
    second_artifact_root: Path,
    first_public_family_registry: Mapping[str, Mapping[str, Any]],
    second_public_family_registry: Mapping[str, Mapping[str, Any]],
) -> None:
    """Reject any route-owned semantic sample/artifact reuse by A1 and B.

    Canonical public-family payloads are intentionally allowed to overlap:
    they are neutral poker facts.  Every generator/config/seed/checkpoint/PBS/
    trajectory/rollout/source-copy byte sequence is route-owned and therefore
    must be disjoint.
    """

    first_authority.validated()
    second_authority.validated()
    if {first_authority.route_domain, second_authority.route_domain} != ROUTE_DOMAINS:
        raise ValueError("cross-route check requires exactly the registered A1 and B routes")
    first_journal.validated(first_artifact_root, first_public_family_registry)
    second_journal.validated(second_artifact_root, second_public_family_registry)
    for authority, journal in (
        (first_authority, first_journal),
        (second_authority, second_journal),
    ):
        if authority.route_domain != journal.route_domain:
            raise ValueError("cross-route authority does not bind its journal route")
        if authority.generator_journal_sha256 != journal.digest:
            raise ValueError("cross-route authority does not bind its generator journal")
        if (
            authority.artifact_registry_sha256
            != journal_artifact_registry_sha256(journal)
        ):
            raise ValueError("cross-route authority does not bind its artifact registry")
    for field in (
        "route_contract_sha256",
        "prelabel_manifest_sha256",
        "generator_journal_sha256",
        "artifact_registry_sha256",
        "provenance_graph_sha256",
        "record_set_sha256",
        "route_salt_sha256",
    ):
        if getattr(first_authority, field) == getattr(second_authority, field):
            raise ValueError(f"independent routes reused {field}")
    shared_samples = {
        event.sample_id for event in first_journal.events
    }.intersection(event.sample_id for event in second_journal.events)
    if shared_samples:
        raise ValueError("independent routes reused semantic pre-label sample IDs")
    first_assets = semantic_artifact_inventory(first_journal)
    second_assets = semantic_artifact_inventory(second_journal)
    for kind in sorted(first_assets):
        if first_assets[kind].intersection(second_assets[kind]):
            raise ValueError(
                f"independent routes reused route-owned {kind} artifact content"
            )
    neutral_public_families = set(first_public_family_registry).intersection(
        second_public_family_registry
    )
    cross_kind_overlap = route_owned_content_inventory(first_journal).intersection(
        route_owned_content_inventory(second_journal)
    ) - neutral_public_families
    if cross_kind_overlap:
        raise ValueError(
            "independent routes reused route-owned bytes/evidence across artifact kinds"
        )
