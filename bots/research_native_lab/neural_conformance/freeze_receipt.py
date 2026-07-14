"""Append-only pre-label receipts and Git-pinned label authorization.

Freezing and labeling are deliberately separate commits.  A freeze receipt is
created with ``O_EXCL`` while every declared label output is absent.  The label
entry point later accepts only a receipt read from an explicitly pinned
ancestor Git commit; a manifest cannot feed its own freshly rebuilt digest back
as authority.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
import stat
import subprocess
from typing import Any, Mapping, Sequence

from .prelabel import PreLabelGeneratorJournal
from .split import (
    SplitAuthority,
    build_leakage_closed_split,
)


PRELABEL_FREEZE_RECEIPT_SCHEMA = "neural-prelabel-no-clobber-receipt-v1"
GIT_RECEIPT_PIN_SCHEMA = "neural-prelabel-git-receipt-pin-v1"


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


def _relative(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\\" in value
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError(f"{label} must be printable traversal-free POSIX text")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in ("", ".", "..") for part in parsed.parts):
        raise ValueError(f"{label} must be relative and traversal-free")
    if len(value.encode("ascii")) > 512:
        raise ValueError(f"{label} is too long")
    return value


def _ensure_real_directory(path: Path, *, create: bool) -> Path:
    path = Path(path)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("receipt directory must be a real directory")
    current = path
    while current != current.parent:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("receipt directory ancestry contains a symlink")
        current = current.parent
    return path


def _stable_read(path: Path) -> bytes:
    path = Path(path)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("receipt parent must be a real directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("receipt cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("receipt must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
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
            raise ValueError("receipt changed during stable read")
        payload = b"".join(chunks)
        if len(payload) != before.st_size:
            raise ValueError("receipt size changed during stable read")
        return payload
    finally:
        os.close(descriptor)


def _strict_json(raw: bytes) -> object:
    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate receipt JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs_hook,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite receipt JSON value: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("receipt is not strict UTF-8 JSON") from exc


def _receipt_path(store_root: Path, route_domain: str, round_index: int) -> Path:
    route_directory = route_domain.replace(":", "_")
    return store_root / route_directory / f"round-{round_index:06d}.json"


def _absent_paths(workspace_root: Path, values: Sequence[str]) -> tuple[str, ...]:
    if type(values) not in (tuple, list) or not values:
        raise ValueError("freeze requires at least one prospective label-output path")
    normalized = tuple(sorted(_relative(value, "label-output path") for value in values))
    if len(normalized) != len(set(normalized)):
        raise ValueError("label-output paths must be unique")
    workspace_root = _ensure_real_directory(workspace_root, create=False)
    for relative in normalized:
        current = workspace_root
        parts = PurePosixPath(relative).parts
        for index, part in enumerate(parts):
            candidate = current / part
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("label-output path crosses a symlink")
            if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("label-output path crosses a non-directory")
            current = candidate
        else:
            raise ValueError("label output already exists before pre-label freeze")
    return normalized


@dataclass(frozen=True, slots=True)
class PreLabelFreezeReceipt:
    route_domain: str
    round_index: int
    authority_sha256: str
    route_contract_sha256: str
    generator_journal_sha256: str
    artifact_registry_sha256: str
    provenance_graph_sha256: str
    public_family_registry_sha256: str
    record_set_sha256: str
    route_salt_sha256: str
    basis_points: dict[str, int]
    minima: dict[str, int]
    seed_root_semantic_ids: tuple[str, ...]
    labels_absent_relative_paths: tuple[str, ...]
    previous_receipt_sha256: str | None
    previous_authority_sha256: str | None
    receipt_sha256: str
    schema: str = PRELABEL_FREEZE_RECEIPT_SCHEMA

    def _content(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("receipt_sha256")
        return payload

    def validated(self) -> "PreLabelFreezeReceipt":
        if self.schema != PRELABEL_FREEZE_RECEIPT_SCHEMA:
            raise ValueError("pre-label freeze receipt schema differs")
        if self.route_domain not in ("route-a1:m5b", "route-b:m5"):
            raise ValueError("pre-label freeze receipt route is not registered")
        if type(self.round_index) is not int or self.round_index < 0:
            raise ValueError("freeze round index must be a nonnegative exact integer")
        for field in (
            "authority_sha256",
            "route_contract_sha256",
            "generator_journal_sha256",
            "artifact_registry_sha256",
            "provenance_graph_sha256",
            "public_family_registry_sha256",
            "record_set_sha256",
            "route_salt_sha256",
            "receipt_sha256",
        ):
            _sha256(getattr(self, field), field)
        for field in ("previous_receipt_sha256", "previous_authority_sha256"):
            value = getattr(self, field)
            if value is not None:
                _sha256(value, field)
        if self.round_index == 0:
            if self.previous_receipt_sha256 is not None or self.previous_authority_sha256 is not None:
                raise ValueError("first freeze round cannot claim a predecessor")
        elif self.previous_receipt_sha256 is None or self.previous_authority_sha256 is None:
            raise ValueError("freeze extension requires one exact predecessor")
        if type(self.basis_points) is not dict or set(self.basis_points) != {
            "train",
            "validation",
            "test",
        } or any(type(value) is not int or value <= 0 for value in self.basis_points.values()) or sum(self.basis_points.values()) != 10_000:
            raise ValueError("freeze receipt split basis differs")
        if type(self.minima) is not dict or set(self.minima) != {
            "component_count",
            "family_count",
            "sample_count",
        } or any(type(value) is not int or value <= 0 for value in self.minima.values()):
            raise ValueError("freeze receipt minima differ")
        if type(self.seed_root_semantic_ids) is not tuple or not self.seed_root_semantic_ids:
            raise ValueError("freeze receipt requires seed-root semantic IDs")
        if tuple(sorted(self.seed_root_semantic_ids)) != self.seed_root_semantic_ids:
            raise ValueError("freeze receipt seed roots must be sorted")
        for value in self.seed_root_semantic_ids:
            _sha256(value, "seed-root semantic ID")
        if type(self.labels_absent_relative_paths) is not tuple or not self.labels_absent_relative_paths:
            raise ValueError("freeze receipt requires absent label paths")
        normalized_paths = tuple(
            sorted(
                _relative(value, "receipt label-output path")
                for value in self.labels_absent_relative_paths
            )
        )
        if normalized_paths != self.labels_absent_relative_paths or len(normalized_paths) != len(set(normalized_paths)):
            raise ValueError("freeze receipt label-output paths are not canonical")
        if self.receipt_sha256 != _digest(self._content()):
            raise ValueError("freeze receipt digest differs from content")
        return self

    def to_payload(self) -> dict[str, Any]:
        self.validated()
        payload = asdict(self)
        payload["seed_root_semantic_ids"] = list(self.seed_root_semantic_ids)
        payload["labels_absent_relative_paths"] = list(
            self.labels_absent_relative_paths
        )
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> "PreLabelFreezeReceipt":
        fields = {field.name for field in cls.__dataclass_fields__.values()}
        if type(payload) is not dict or set(payload) != fields:
            raise ValueError("pre-label freeze receipt fields differ")
        converted = dict(payload)
        if type(converted["seed_root_semantic_ids"]) is not list or type(
            converted["labels_absent_relative_paths"]
        ) is not list:
            raise ValueError("freeze receipt tuple fields must serialize as lists")
        converted["seed_root_semantic_ids"] = tuple(
            converted["seed_root_semantic_ids"]
        )
        converted["labels_absent_relative_paths"] = tuple(
            converted["labels_absent_relative_paths"]
        )
        return cls(**converted).validated()


def read_freeze_receipt(path: Path) -> PreLabelFreezeReceipt:
    raw = _stable_read(path)
    receipt = PreLabelFreezeReceipt.from_payload(_strict_json(raw))
    if raw != _canonical_bytes(receipt.to_payload()):
        raise ValueError("freeze receipt bytes are not canonical JSON")
    return receipt


def freeze_prelabel_receipt(
    store_root: Path,
    workspace_root: Path,
    journal: PreLabelGeneratorJournal,
    artifact_root: Path,
    public_family_registry: Mapping[str, Mapping[str, Any]],
    *,
    authority: SplitAuthority,
    route_salt: str,
    round_index: int,
    labels_absent_relative_paths: Sequence[str],
    expected_previous_receipt_sha256: str | None,
) -> tuple[PreLabelFreezeReceipt, Path]:
    """Write the unique receipt for one route/round, refusing all overwrite."""

    if type(round_index) is not int or round_index < 0:
        raise ValueError("freeze round index must be a nonnegative exact integer")
    authority = authority.validated()
    # This recomputation also stable-reads every pre-label artifact.
    build_leakage_closed_split(
        journal,
        artifact_root,
        public_family_registry,
        authority=authority,
        route_salt=route_salt,
    )
    absent = _absent_paths(workspace_root, labels_absent_relative_paths)
    store_root = _ensure_real_directory(store_root, create=True)
    route_root = _ensure_real_directory(
        store_root / authority.route_domain.replace(":", "_"), create=True
    )
    lock_path = route_root / ".freeze.lock"
    lock_descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        target = _receipt_path(store_root, authority.route_domain, round_index)
        previous: PreLabelFreezeReceipt | None = None
        if round_index == 0:
            if expected_previous_receipt_sha256 is not None:
                raise ValueError("first freeze round cannot claim a predecessor")
        else:
            _sha256(
                expected_previous_receipt_sha256,
                "expected_previous_receipt_sha256",
            )
            previous_path = _receipt_path(
                store_root, authority.route_domain, round_index - 1
            )
            previous = read_freeze_receipt(previous_path)
            if previous.receipt_sha256 != expected_previous_receipt_sha256:
                raise ValueError("freeze extension predecessor differs from CAS head")
            if previous.route_domain != authority.route_domain:
                raise ValueError("freeze extension route differs from predecessor")
            # No skipped/forked successor is possible through canonical slots.
            later = _receipt_path(store_root, authority.route_domain, round_index + 1)
            if later.exists() or later.is_symlink():
                raise ValueError("freeze receipt chain contains a non-canonical successor")

        artifacts = journal.artifact_map()
        seed_roots = tuple(
            sorted(
                {
                    artifacts[event.seed_root_artifact_id].semantic_id
                    for event in journal.events
                }
            )
        )
        base = {
            "route_domain": authority.route_domain,
            "round_index": round_index,
            "authority_sha256": authority.digest,
            "route_contract_sha256": authority.route_contract_sha256,
            "generator_journal_sha256": authority.generator_journal_sha256,
            "artifact_registry_sha256": authority.artifact_registry_sha256,
            "provenance_graph_sha256": authority.provenance_graph_sha256,
            "public_family_registry_sha256": authority.public_family_registry_sha256,
            "record_set_sha256": authority.record_set_sha256,
            "route_salt_sha256": authority.route_salt_sha256,
            "basis_points": authority.basis_points,
            "minima": {
                "component_count": authority.minimum_components_per_split,
                "family_count": authority.minimum_families_per_split,
                "sample_count": authority.minimum_samples_per_split,
            },
            "seed_root_semantic_ids": seed_roots,
            "labels_absent_relative_paths": absent,
            "previous_receipt_sha256": (
                None if previous is None else previous.receipt_sha256
            ),
            "previous_authority_sha256": (
                None if previous is None else previous.authority_sha256
            ),
            "schema": PRELABEL_FREEZE_RECEIPT_SCHEMA,
        }
        receipt = PreLabelFreezeReceipt(
            receipt_sha256=_digest(base), **base
        ).validated()
        raw = _canonical_bytes(receipt.to_payload())
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(target, flags, 0o444)
        except FileExistsError as exc:
            raise ValueError("pre-label receipt already exists for this route/round") from exc
        try:
            written = 0
            while written < len(raw):
                written += os.write(descriptor, raw[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_descriptor = os.open(route_root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return receipt, target
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


@dataclass(frozen=True, slots=True)
class GitReceiptPin:
    commit_sha: str
    receipt_relative_path: str
    receipt_sha256: str
    route_domain: str
    round_index: int
    schema: str = GIT_RECEIPT_PIN_SCHEMA

    def validated(self) -> "GitReceiptPin":
        if self.schema != GIT_RECEIPT_PIN_SCHEMA:
            raise ValueError("Git receipt pin schema differs")
        if (
            type(self.commit_sha) is not str
            or len(self.commit_sha) not in (40, 64)
            or any(character not in "0123456789abcdef" for character in self.commit_sha)
        ):
            raise ValueError("Git receipt pin requires a full lowercase commit ID")
        _relative(self.receipt_relative_path, "Git receipt path")
        _sha256(self.receipt_sha256, "Git-pinned receipt SHA-256")
        if self.route_domain not in ("route-a1:m5b", "route-b:m5"):
            raise ValueError("Git receipt route is not registered")
        if type(self.round_index) is not int or self.round_index < 0:
            raise ValueError("Git receipt round must be a nonnegative exact integer")
        return self


def _git(repo_root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise ValueError(
            "Git receipt verification failed: "
            + result.stderr.decode("utf-8", "replace").strip()
        )
    return result


def verify_git_pinned_receipt(
    repo_root: Path,
    *,
    commit_sha: str,
    receipt_relative_path: str,
    expected_receipt_sha256: str,
    expected_route_domain: str,
    expected_round_index: int,
) -> GitReceiptPin:
    """Read a receipt from an ancestor commit and prove labels were not in it."""

    repo_root = _ensure_real_directory(repo_root, create=False)
    candidate = GitReceiptPin(
        commit_sha=commit_sha,
        receipt_relative_path=_relative(
            receipt_relative_path, "Git receipt path"
        ),
        receipt_sha256=_sha256(
            expected_receipt_sha256, "expected Git receipt SHA-256"
        ),
        route_domain=expected_route_domain,
        round_index=expected_round_index,
    ).validated()
    resolved = _git(repo_root, "rev-parse", f"{candidate.commit_sha}^{{commit}}").stdout.decode().strip()
    if resolved != candidate.commit_sha:
        raise ValueError("Git receipt pin does not name the exact commit object")
    if _git(
        repo_root,
        "merge-base",
        "--is-ancestor",
        candidate.commit_sha,
        "HEAD",
        check=False,
    ).returncode != 0:
        raise ValueError("Git receipt commit is not an ancestor of current HEAD")
    raw = _git(
        repo_root,
        "show",
        f"{candidate.commit_sha}:{candidate.receipt_relative_path}",
    ).stdout
    receipt = PreLabelFreezeReceipt.from_payload(_strict_json(raw))
    if raw != _canonical_bytes(receipt.to_payload()):
        raise ValueError("Git-pinned receipt bytes are not canonical")
    if (
        receipt.receipt_sha256 != candidate.receipt_sha256
        or receipt.route_domain != candidate.route_domain
        or receipt.round_index != candidate.round_index
    ):
        raise ValueError("Git-pinned receipt identity differs from expected contract")
    for relative in receipt.labels_absent_relative_paths:
        listing = _git(
            repo_root,
            "ls-tree",
            "-r",
            "--name-only",
            candidate.commit_sha,
            "--",
            relative,
        ).stdout
        if listing.strip():
            raise ValueError("Git receipt commit already contains a label output")
    current = read_freeze_receipt(repo_root / candidate.receipt_relative_path)
    if current.to_payload() != receipt.to_payload():
        raise ValueError("working receipt differs from Git-pinned receipt")
    return candidate


def verify_label_authorization(
    pin: GitReceiptPin,
    receipt: PreLabelFreezeReceipt,
    authority: SplitAuthority,
    journal: PreLabelGeneratorJournal,
    artifact_root: Path,
    public_family_registry: Mapping[str, Mapping[str, Any]],
    workspace_root: Path,
    *,
    route_salt: str,
) -> None:
    """Authorize first label creation only from a separately verified Git pin."""

    if type(pin) is not GitReceiptPin:
        raise TypeError("label authorization requires an exact verified GitReceiptPin")
    pin.validated()
    receipt.validated()
    authority.validated()
    if (
        pin.receipt_sha256 != receipt.receipt_sha256
        or pin.route_domain != receipt.route_domain
        or pin.round_index != receipt.round_index
        or receipt.authority_sha256 != authority.digest
        or receipt.generator_journal_sha256 != journal.digest
    ):
        raise ValueError("label authorization receipt/authority/journal identity differs")
    # Re-check prospective outputs immediately before the label writer opens them.
    _absent_paths(workspace_root, receipt.labels_absent_relative_paths)
    build_leakage_closed_split(
        journal,
        artifact_root,
        public_family_registry,
        authority=authority,
        route_salt=route_salt,
    )

