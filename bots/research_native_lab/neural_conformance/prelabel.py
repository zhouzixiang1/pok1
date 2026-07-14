"""Typed, file-backed pre-label provenance for independent neural routes.

The split verifier never accepts caller-invented relation digests.  Every
sample is replayed from one typed generator event whose referenced artifacts
are stable-read and content-hashed before labels may exist.  Route-neutral
semantic IDs make copied bytes visible even when a second route renames files
or changes its route namespace.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import stat
import re
from typing import Any, Mapping

from .public_family import public_family_payload_id, validate_public_family_payload


PRELABEL_ARTIFACT_SCHEMA = "neural-prelabel-artifact-v1"
PRELABEL_EVENT_SCHEMA = "neural-prelabel-generator-event-v1"
PRELABEL_JOURNAL_SCHEMA = "neural-prelabel-generator-journal-v1"
ROUTE_DOMAINS = frozenset({"route-a1:m5b", "route-b:m5"})
ARTIFACT_KINDS = frozenset(
    {
        "route-contract",
        "generator-code",
        "generator-config",
        "seed-root",
        "source-checkpoint",
        "pbs-input",
        "trajectory",
        "rollout",
        "source-copy",
    }
)

_EVENT_ARTIFACT_FIELDS = {
    "route_contract_artifact_id": "route-contract",
    "generator_code_artifact_id": "generator-code",
    "generator_config_artifact_id": "generator-config",
    "seed_root_artifact_id": "seed-root",
    "source_checkpoint_artifact_id": "source-checkpoint",
    "pbs_artifact_id": "pbs-input",
    "trajectory_artifact_id": "trajectory",
    "rollout_artifact_id": "rollout",
    "source_copy_artifact_id": "source-copy",
}

PRELABEL_ARTIFACT_CONTENT_SCHEMA = "neural-prelabel-artifact-content-v1"
_ARTIFACT_PAYLOAD_FIELDS = {
    "route-contract": {"contract_sha256", "contract_byte_count"},
    "generator-code": {"source_closure_sha256", "source_file_count"},
    "generator-config": {"config_sha256"},
    "seed-root": {"seed_root_sha256", "seed_count"},
    "source-checkpoint": {"checkpoint_sha256", "checkpoint_byte_count"},
    "pbs-input": {
        "public_family_id",
        "public_input_sha256",
        "range_payload_sha256",
        "legal_mask_sha256",
        "semantics_sha256",
    },
    "trajectory": {"trajectory_sha256", "deal_key_sha256", "decision_count"},
    "rollout": {"rollout_plan_sha256", "seed_block_sha256", "rollout_count"},
    "source-copy": {"origin_sha256", "copy_ordinal"},
}
_COUNT_FIELDS = frozenset(
    {
        "contract_byte_count",
        "source_file_count",
        "seed_count",
        "checkpoint_byte_count",
        "decision_count",
        "rollout_count",
        "copy_ordinal",
    }
)
_FORBIDDEN_PRELABEL_KEY_PARTS = frozenset(
    {
        "label",
        "labels",
        "outcome",
        "payoff",
        "utility",
        "reward",
        "winner",
        "earnings",
        "target",
        "target_cfv",
        "model_output",
        "test_result",
        "validation_result",
    }
)
_FORBIDDEN_TEXT = re.compile(
    r"(?:^|[^a-z])(label|labels|outcome|payoff|utility|reward|winner|earnings|target|target_cfv|model_output)(?:$|[^a-z])"
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


def _sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _route(value: object) -> str:
    if type(value) is not str or value not in ROUTE_DOMAINS:
        raise ValueError("pre-label route is not registered")
    return value


def _strict_json(raw: bytes) -> object:
    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise ValueError("pre-label artifact JSON has a duplicate/non-string key")
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs_hook,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite pre-label artifact JSON value: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("pre-label artifact must be strict canonical UTF-8 JSON") from exc


def _assert_outcome_free(value: object, *, path: str = "payload") -> None:
    if type(value) is dict:
        for key, child in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in _FORBIDDEN_PRELABEL_KEY_PARTS:
                raise ValueError(f"pre-label artifact contains forbidden outcome/label key {path}.{key}")
            _assert_outcome_free(child, path=f"{path}.{key}")
    elif type(value) is list:
        for index, child in enumerate(value):
            _assert_outcome_free(child, path=f"{path}[{index}]")
    elif type(value) is str:
        if _FORBIDDEN_TEXT.search(value.lower()):
            raise ValueError(f"pre-label artifact contains forbidden outcome/label text at {path}")
    elif value is not None and type(value) not in (int, float, bool):
        raise ValueError(f"pre-label artifact contains a non-canonical scalar at {path}")
    if type(value) is float and not math.isfinite(value):
        raise ValueError(f"pre-label artifact contains a non-finite number at {path}")


def _validate_artifact_content(raw: bytes, kind: str) -> tuple[dict[str, Any], tuple[str, ...]]:
    if len(raw) > 1024 * 1024:
        raise ValueError("pre-label evidence envelope exceeds one MiB")
    payload = _strict_json(raw)
    if type(payload) is not dict or set(payload) != {"kind", "payload", "schema"}:
        raise ValueError("pre-label artifact evidence-envelope fields differ")
    if payload["schema"] != PRELABEL_ARTIFACT_CONTENT_SCHEMA or payload["kind"] != kind:
        raise ValueError("pre-label artifact evidence-envelope schema/kind differs")
    facts = payload["payload"]
    expected_fields = _ARTIFACT_PAYLOAD_FIELDS[kind]
    if type(facts) is not dict or set(facts) != expected_fields:
        raise ValueError(f"pre-label {kind} evidence fields differ")
    _assert_outcome_free(facts)
    evidence: list[str] = []
    for field, value in facts.items():
        if field in _COUNT_FIELDS:
            if type(value) is not int or value < 0:
                raise ValueError(f"pre-label {kind}.{field} must be a nonnegative exact integer")
        else:
            evidence.append(_sha256(value, f"pre-label {kind}.{field}"))
    if kind == "pbs-input" and facts["public_family_id"] in {
        facts["public_input_sha256"],
        facts["range_payload_sha256"],
        facts["legal_mask_sha256"],
        facts["semantics_sha256"],
    }:
        raise ValueError("PBS evidence namespaces collapsed onto one digest")
    if raw != _canonical_bytes(payload):
        raise ValueError("pre-label artifact evidence envelope is not canonical JSON")
    return payload, tuple(sorted(set(evidence)))


def _relative_path(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\\" in value
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("pre-label artifact path must be bounded printable POSIX text")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or parsed.parts != tuple(part for part in parsed.parts if part not in ("", ".", "..")):
        raise ValueError("pre-label artifact path must be relative and traversal-free")
    if len(value.encode("ascii")) > 512:
        raise ValueError("pre-label artifact path is too long")
    return value


def _artifact_semantic_id(kind: str, content_sha256: str, byte_count: int) -> str:
    return _digest(
        {
            "byte_count": byte_count,
            "content_sha256": content_sha256,
            "kind": kind,
            "schema": "neural-prelabel-artifact-semantic-id-v1",
        }
    )


def _artifact_instance_id(
    route_domain: str,
    kind: str,
    relative_path: str,
    semantic_id: str,
) -> str:
    return _digest(
        {
            "kind": kind,
            "relative_path": relative_path,
            "route_domain": route_domain,
            "schema": "neural-prelabel-artifact-instance-id-v1",
            "semantic_id": semantic_id,
        }
    )


def _stable_file_digest(root: Path, relative_path: str) -> tuple[str, int, bytes]:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("pre-label artifact root must be a real directory")
    current = root
    parts = PurePosixPath(relative_path).parts
    for part in parts[:-1]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise ValueError("pre-label artifact path is missing") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("pre-label artifact path crosses a symlink/non-directory")
    path = root.joinpath(*parts)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("pre-label artifact cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("pre-label artifact must be a regular file")
        digest = hashlib.sha256()
        total = 0
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            total += len(block)
            chunks.append(block)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("pre-label artifact changed during stable read")
        if total != before.st_size:
            raise ValueError("pre-label artifact size changed during read")
        return digest.hexdigest(), total, b"".join(chunks)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class PreLabelArtifact:
    route_domain: str
    kind: str
    relative_path: str
    content_sha256: str
    byte_count: int
    evidence_sha256s: tuple[str, ...]
    semantic_id: str
    instance_id: str
    schema: str = PRELABEL_ARTIFACT_SCHEMA

    @classmethod
    def from_file(
        cls,
        root: Path,
        *,
        route_domain: str,
        kind: str,
        relative_path: str,
    ) -> "PreLabelArtifact":
        route_domain = _route(route_domain)
        if type(kind) is not str or kind not in ARTIFACT_KINDS:
            raise ValueError("pre-label artifact kind is invalid")
        relative_path = _relative_path(relative_path)
        content_sha256, byte_count, raw = _stable_file_digest(root, relative_path)
        if byte_count <= 0:
            raise ValueError("pre-label artifacts must be nonempty")
        _, evidence_sha256s = _validate_artifact_content(raw, kind)
        semantic_id = _artifact_semantic_id(kind, content_sha256, byte_count)
        return cls(
            route_domain=route_domain,
            kind=kind,
            relative_path=relative_path,
            content_sha256=content_sha256,
            byte_count=byte_count,
            evidence_sha256s=evidence_sha256s,
            semantic_id=semantic_id,
            instance_id=_artifact_instance_id(
                route_domain, kind, relative_path, semantic_id
            ),
        ).validated()

    def validated(self) -> "PreLabelArtifact":
        if self.schema != PRELABEL_ARTIFACT_SCHEMA:
            raise ValueError("pre-label artifact schema differs")
        route_domain = _route(self.route_domain)
        if type(self.kind) is not str or self.kind not in ARTIFACT_KINDS:
            raise ValueError("pre-label artifact kind is invalid")
        relative_path = _relative_path(self.relative_path)
        content_sha256 = _sha256(self.content_sha256, "artifact content_sha256")
        if type(self.byte_count) is not int or self.byte_count <= 0:
            raise ValueError("pre-label artifact byte_count must be a positive exact integer")
        if (
            type(self.evidence_sha256s) is not tuple
            or not self.evidence_sha256s
            or tuple(sorted(self.evidence_sha256s)) != self.evidence_sha256s
            or len(self.evidence_sha256s) != len(set(self.evidence_sha256s))
        ):
            raise ValueError("pre-label artifact evidence digests must be a nonempty sorted tuple")
        for evidence in self.evidence_sha256s:
            _sha256(evidence, "artifact evidence SHA-256")
        expected_semantic = _artifact_semantic_id(
            self.kind, content_sha256, self.byte_count
        )
        if _sha256(self.semantic_id, "artifact semantic_id") != expected_semantic:
            raise ValueError("pre-label artifact semantic ID is not content-derived")
        expected_instance = _artifact_instance_id(
            route_domain, self.kind, relative_path, expected_semantic
        )
        if _sha256(self.instance_id, "artifact instance_id") != expected_instance:
            raise ValueError("pre-label artifact instance ID is not content-derived")
        return self

    def verify_file(self, root: Path) -> dict[str, Any]:
        self.validated()
        observed_digest, observed_size, raw = _stable_file_digest(root, self.relative_path)
        if (observed_digest, observed_size) != (self.content_sha256, self.byte_count):
            raise ValueError("pre-label artifact bytes differ from journal")
        envelope, observed_evidence = _validate_artifact_content(raw, self.kind)
        if observed_evidence != self.evidence_sha256s:
            raise ValueError("pre-label artifact evidence digests differ from journal")
        return envelope["payload"]

    def to_payload(self) -> dict[str, Any]:
        self.validated()
        return {
            "byte_count": self.byte_count,
            "content_sha256": self.content_sha256,
            "evidence_sha256s": list(self.evidence_sha256s),
            "instance_id": self.instance_id,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "route_domain": self.route_domain,
            "schema": self.schema,
            "semantic_id": self.semantic_id,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "PreLabelArtifact":
        if type(payload) is not dict or set(payload) != {
            "byte_count",
            "content_sha256",
            "evidence_sha256s",
            "instance_id",
            "kind",
            "relative_path",
            "route_domain",
            "schema",
            "semantic_id",
        }:
            raise ValueError("pre-label artifact payload fields differ")
        if type(payload["evidence_sha256s"]) is not list:
            raise ValueError("pre-label artifact evidence digests must serialize as a list")
        converted = dict(payload)
        converted["evidence_sha256s"] = tuple(converted["evidence_sha256s"])
        return cls(**converted).validated()


@dataclass(frozen=True, slots=True)
class PreLabelGeneratorEvent:
    route_domain: str
    event_index: int
    sample_id: str
    public_family_id: str
    route_contract_artifact_id: str
    generator_code_artifact_id: str
    generator_config_artifact_id: str
    seed_root_artifact_id: str
    source_checkpoint_artifact_id: str
    pbs_artifact_id: str
    trajectory_artifact_id: str
    rollout_artifact_id: str
    source_copy_artifact_id: str
    augmentation_kind: str
    augmentation_parent_sample_id: str | None
    decision_index: int
    seed_counter: int
    schema: str = PRELABEL_EVENT_SCHEMA

    def _semantic_payload(
        self, artifacts: Mapping[str, PreLabelArtifact]
    ) -> dict[str, Any]:
        return {
            "artifacts": {
                field: artifacts[getattr(self, field)].semantic_id
                for field in sorted(_EVENT_ARTIFACT_FIELDS)
            },
            "augmentation_kind": self.augmentation_kind,
            "augmentation_parent_sample_id": self.augmentation_parent_sample_id,
            "decision_index": self.decision_index,
            "public_family_id": self.public_family_id,
            "schema": "neural-prelabel-sample-semantic-id-v1",
            "seed_counter": self.seed_counter,
        }

    def expected_sample_id(
        self, artifacts: Mapping[str, PreLabelArtifact]
    ) -> str:
        return _digest(self._semantic_payload(artifacts))

    def validated(
        self,
        artifacts: Mapping[str, PreLabelArtifact],
    ) -> "PreLabelGeneratorEvent":
        if self.schema != PRELABEL_EVENT_SCHEMA:
            raise ValueError("pre-label generator-event schema differs")
        _route(self.route_domain)
        if type(self.event_index) is not int or self.event_index < 0:
            raise ValueError("pre-label event index must be a nonnegative exact integer")
        _sha256(self.public_family_id, "event public_family_id")
        if type(self.decision_index) is not int or self.decision_index < 0:
            raise ValueError("event decision_index must be a nonnegative exact integer")
        if type(self.seed_counter) is not int or self.seed_counter < 0:
            raise ValueError("event seed_counter must be a nonnegative exact integer")
        if self.augmentation_kind not in ("base", "derived"):
            raise ValueError("event augmentation_kind must be base or derived")
        if self.augmentation_kind == "base":
            if self.augmentation_parent_sample_id is not None:
                raise ValueError("base event must carry a typed absent augmentation parent")
        else:
            _sha256(
                self.augmentation_parent_sample_id,
                "augmentation_parent_sample_id",
            )
        for field, expected_kind in _EVENT_ARTIFACT_FIELDS.items():
            reference = _sha256(getattr(self, field), field)
            artifact = artifacts.get(reference)
            if artifact is None:
                raise ValueError(f"event {field} is absent from artifact registry")
            if artifact.route_domain != self.route_domain or artifact.kind != expected_kind:
                raise ValueError(f"event {field} has the wrong route or artifact kind")
        if _sha256(self.sample_id, "event sample_id") != self.expected_sample_id(artifacts):
            raise ValueError("event sample ID is not replay-derived from artifact semantics")
        return self

    def to_payload(self) -> dict[str, Any]:
        return {
            "augmentation_kind": self.augmentation_kind,
            "augmentation_parent_sample_id": self.augmentation_parent_sample_id,
            "decision_index": self.decision_index,
            "event_index": self.event_index,
            "generator_code_artifact_id": self.generator_code_artifact_id,
            "generator_config_artifact_id": self.generator_config_artifact_id,
            "pbs_artifact_id": self.pbs_artifact_id,
            "public_family_id": self.public_family_id,
            "route_contract_artifact_id": self.route_contract_artifact_id,
            "rollout_artifact_id": self.rollout_artifact_id,
            "route_domain": self.route_domain,
            "sample_id": self.sample_id,
            "schema": self.schema,
            "seed_counter": self.seed_counter,
            "seed_root_artifact_id": self.seed_root_artifact_id,
            "source_checkpoint_artifact_id": self.source_checkpoint_artifact_id,
            "source_copy_artifact_id": self.source_copy_artifact_id,
            "trajectory_artifact_id": self.trajectory_artifact_id,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "PreLabelGeneratorEvent":
        expected = {
            "augmentation_kind",
            "augmentation_parent_sample_id",
            "decision_index",
            "event_index",
            "generator_code_artifact_id",
            "generator_config_artifact_id",
            "pbs_artifact_id",
            "public_family_id",
            "route_contract_artifact_id",
            "rollout_artifact_id",
            "route_domain",
            "sample_id",
            "schema",
            "seed_counter",
            "seed_root_artifact_id",
            "source_checkpoint_artifact_id",
            "source_copy_artifact_id",
            "trajectory_artifact_id",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise ValueError("pre-label generator-event payload fields differ")
        return cls(**payload)


def make_generator_event(
    *,
    route_domain: str,
    event_index: int,
    public_family_id: str,
    artifact_ids: Mapping[str, str],
    artifacts: Mapping[str, PreLabelArtifact],
    augmentation_kind: str,
    augmentation_parent_sample_id: str | None,
    decision_index: int,
    seed_counter: int,
) -> PreLabelGeneratorEvent:
    fields = set(_EVENT_ARTIFACT_FIELDS)
    if type(artifact_ids) is not dict or set(artifact_ids) != fields:
        raise ValueError("generator event artifact reference fields differ")
    provisional = PreLabelGeneratorEvent(
        route_domain=route_domain,
        event_index=event_index,
        sample_id="0" * 64,
        public_family_id=public_family_id,
        augmentation_kind=augmentation_kind,
        augmentation_parent_sample_id=augmentation_parent_sample_id,
        decision_index=decision_index,
        seed_counter=seed_counter,
        **artifact_ids,
    )
    event = PreLabelGeneratorEvent(
        **{
            **provisional.to_payload(),
            "sample_id": provisional.expected_sample_id(artifacts),
        }
    )
    return event.validated(artifacts)


@dataclass(frozen=True, slots=True)
class PreLabelGeneratorJournal:
    route_domain: str
    artifacts: tuple[PreLabelArtifact, ...]
    events: tuple[PreLabelGeneratorEvent, ...]
    schema: str = PRELABEL_JOURNAL_SCHEMA

    def validated(
        self,
        artifact_root: Path,
        public_family_registry: Mapping[str, Mapping[str, Any]],
    ) -> "PreLabelGeneratorJournal":
        if self.schema != PRELABEL_JOURNAL_SCHEMA:
            raise ValueError("pre-label journal schema differs")
        route_domain = _route(self.route_domain)
        if type(self.artifacts) is not tuple or not self.artifacts:
            raise ValueError("pre-label journal requires an immutable artifact tuple")
        if type(self.events) is not tuple or not self.events:
            raise ValueError("pre-label journal requires an immutable event tuple")
        artifacts: dict[str, PreLabelArtifact] = {}
        artifact_facts: dict[str, dict[str, Any]] = {}
        paths: set[str] = set()
        semantic_keys: set[tuple[str, str]] = set()
        for artifact in self.artifacts:
            if type(artifact) is not PreLabelArtifact:
                raise TypeError("journal artifacts must be exact PreLabelArtifact values")
            artifact.validated()
            if artifact.route_domain != route_domain:
                raise ValueError("journal artifact route differs")
            if artifact.instance_id in artifacts or artifact.relative_path in paths:
                raise ValueError("journal artifact instance/path is duplicated")
            semantic_key = (artifact.kind, artifact.semantic_id)
            if semantic_key in semantic_keys:
                raise ValueError("journal duplicates one semantic artifact under another path")
            artifact_facts[artifact.instance_id] = artifact.verify_file(artifact_root)
            artifacts[artifact.instance_id] = artifact
            paths.add(artifact.relative_path)
            semantic_keys.add(semantic_key)
        for singleton_kind in (
            "route-contract",
            "generator-code",
            "generator-config",
            "seed-root",
        ):
            if sum(artifact.kind == singleton_kind for artifact in self.artifacts) != 1:
                raise ValueError(
                    f"journal requires exactly one {singleton_kind} artifact"
                )

        if type(public_family_registry) is not dict or not public_family_registry:
            raise ValueError("journal requires a nonempty exact public-family registry")
        family_ids: set[str] = set()
        for family_id, payload in public_family_registry.items():
            _sha256(family_id, "public-family registry key")
            validated = validate_public_family_payload(payload)
            if public_family_payload_id(validated) != family_id:
                raise ValueError("public-family registry key differs from payload")
            family_ids.add(family_id)
        for artifact in self.artifacts:
            overlap = set(artifact.evidence_sha256s).intersection(family_ids)
            if artifact.kind != "pbs-input" and overlap:
                raise ValueError(
                    "non-PBS artifact reused a neutral public-family digest"
                )

        observed_indices: list[int] = []
        observed_samples: set[str] = set()
        used_artifacts: set[str] = set()
        for event in self.events:
            if type(event) is not PreLabelGeneratorEvent:
                raise TypeError("journal events must be exact generator events")
            event.validated(artifacts)
            if event.route_domain != route_domain:
                raise ValueError("journal event route differs")
            if event.public_family_id not in family_ids:
                raise ValueError("journal event family is absent from registry")
            pbs_facts = artifact_facts[event.pbs_artifact_id]
            if pbs_facts["public_family_id"] != event.public_family_id:
                raise ValueError("PBS artifact public family differs from generator event")
            pbs_artifact = artifacts[event.pbs_artifact_id]
            if set(pbs_artifact.evidence_sha256s).intersection(family_ids) != {
                event.public_family_id
            }:
                raise ValueError(
                    "PBS evidence must contain exactly its neutral public-family digest"
                )
            if event.sample_id in observed_samples:
                raise ValueError("journal sample identity is duplicated")
            if event.augmentation_kind == "derived":
                parent = event.augmentation_parent_sample_id
                if parent not in observed_samples:
                    raise ValueError("augmentation parent must be an earlier journal event")
            observed_indices.append(event.event_index)
            observed_samples.add(event.sample_id)
            used_artifacts.update(
                getattr(event, field) for field in _EVENT_ARTIFACT_FIELDS
            )
        if observed_indices != list(range(len(self.events))):
            raise ValueError("journal event indices must be contiguous and ordered")
        if set(artifacts) != used_artifacts:
            raise ValueError("journal artifact registry must exactly cover event references")
        if family_ids != {event.public_family_id for event in self.events}:
            raise ValueError("public-family registry must exactly cover journal events")
        return self

    def artifact_map(self) -> dict[str, PreLabelArtifact]:
        return {artifact.instance_id: artifact for artifact in self.artifacts}

    def to_payload(self) -> dict[str, Any]:
        return {
            "artifacts": [
                artifact.to_payload()
                for artifact in sorted(self.artifacts, key=lambda item: item.instance_id)
            ],
            "events": [event.to_payload() for event in self.events],
            "route_domain": self.route_domain,
            "schema": self.schema,
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_payload())

    @classmethod
    def from_payload(cls, payload: object) -> "PreLabelGeneratorJournal":
        if type(payload) is not dict or set(payload) != {
            "artifacts",
            "events",
            "route_domain",
            "schema",
        }:
            raise ValueError("pre-label journal payload fields differ")
        if type(payload["artifacts"]) is not list or type(payload["events"]) is not list:
            raise ValueError("pre-label journal collections must be exact lists")
        return cls(
            route_domain=payload["route_domain"],
            artifacts=tuple(
                PreLabelArtifact.from_payload(row) for row in payload["artifacts"]
            ),
            events=tuple(
                PreLabelGeneratorEvent.from_payload(row) for row in payload["events"]
            ),
            schema=payload["schema"],
        )


def journal_artifact_registry_sha256(journal: PreLabelGeneratorJournal) -> str:
    return _digest(
        {
            "artifacts": [
                artifact.to_payload()
                for artifact in sorted(journal.artifacts, key=lambda item: item.instance_id)
            ],
            "schema": "neural-prelabel-artifact-registry-v1",
        }
    )


def semantic_artifact_inventory(
    journal: PreLabelGeneratorJournal,
) -> dict[str, set[str]]:
    result = {kind: set() for kind in ARTIFACT_KINDS}
    for artifact in journal.artifacts:
        artifact.validated()
        result[artifact.kind].add(artifact.semantic_id)
    return result


def route_owned_content_inventory(
    journal: PreLabelGeneratorJournal,
) -> set[str]:
    """All raw envelope and referenced evidence digests, regardless of kind.

    Comparing the kind-neutral closure prevents a copied generator file from
    being hidden by reclassifying it as a seed, checkpoint or other asset.
    """

    inventory: set[str] = set()
    for artifact in journal.artifacts:
        artifact.validated()
        inventory.add(artifact.content_sha256)
        inventory.update(artifact.evidence_sha256s)
    return inventory
