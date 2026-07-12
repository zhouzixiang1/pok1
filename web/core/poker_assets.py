"""System-owned, content-addressed poker precomputation assets.

This module is deliberately separate from generated bots.  The builder is an
offline infrastructure operation; bot candidates and LLM workers are consumers
only.  Loading never regenerates or repairs an asset, and both the manifest and
the immutable blob are published read-only.

The first schema is intentionally modest but real: one fixed-width record for
each of the 1,326 unordered two-card combinations.  It gives consumers an O(1)
mapping from a canonical pair of rank-major card ids to its 169-class metadata.
It is *not* an equity table and makes no claim about strategy or all-in equity.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
from typing import Any, Final


ASSET_ID: Final = "holdem-hole-combo-metadata"
MANIFEST_FORMAT: Final = "pok-system-asset-manifest-v1"
BINARY_FORMAT_VERSION: Final = 1
SCHEMA_VERSION: Final = 1
CARD_COUNT: Final = 52
RANK_COUNT: Final = 13
SUIT_COUNT: Final = 4
CLASS_COUNT: Final = 169
RECORD_COUNT: Final = 1326

MANIFEST_FILENAME: Final = "holdem-hole-combo-metadata-v1.manifest.json"
ARTIFACT_PREFIX: Final = "holdem-hole-combo-metadata-v1"
MAX_MANIFEST_BYTES: Final = 16 * 1024
MAX_ARTIFACT_BYTES: Final = 32 * 1024

_MAGIC: Final = b"POKHCMD\0"
_HEADER = struct.Struct("<8sHHHHIIII32s32s40s24x")
_RECORD = struct.Struct("<8BH4B")
HEADER_SIZE: Final = _HEADER.size
RECORD_SIZE: Final = _RECORD.size
PAYLOAD_SIZE: Final = RECORD_COUNT * RECORD_SIZE
ARTIFACT_SIZE: Final = HEADER_SIZE + PAYLOAD_SIZE

_FLAG_SUITED: Final = 1
_FLAG_PAIR: Final = 2
_HEX_40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_RANK_LABELS: Final = "23456789TJQKA"


class PokerAssetError(RuntimeError):
    """Base error for the system-owned asset boundary."""


class AssetSchemaError(PokerAssetError):
    """The manifest or binary header does not match the frozen schema."""


class AssetIntegrityError(PokerAssetError):
    """A digest, immutable filename, or payload invariant is invalid."""


class AssetGenerationError(PokerAssetError):
    """The offline generator was asked to publish to an unsafe destination."""


@dataclass(frozen=True)
class BuiltPokerAsset:
    """Receipt returned by the offline system builder."""

    manifest_path: Path
    artifact_path: Path
    contract_sha256: str
    payload_sha256: str
    artifact_sha256: str
    generator_commit: str


@dataclass(frozen=True)
class HoleComboMetadata:
    """One decoded fixed-width 1,326-combination record."""

    index: int
    card_a: int
    card_b: int
    rank_a: int
    rank_b: int
    suit_a: int
    suit_b: int
    high_rank: int
    low_rank: int
    class_id: int
    class_row: int
    class_col: int
    suited: bool
    pair: bool
    class_combo_count: int

    @property
    def class_label(self) -> str:
        high = _RANK_LABELS[self.high_rank]
        low = _RANK_LABELS[self.low_rank]
        if self.pair:
            return high + low
        return high + low + ("s" if self.suited else "o")


@dataclass(frozen=True)
class AssetStartupStats:
    """Bounded cold-open accounting; record decoding stays demand-driven."""

    manifest_bytes: int
    mapped_bytes: int
    eager_records_decoded: int
    storage: str = "readonly_mmap"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes | bytearray | memoryview | mmap.mmap) -> str:
    return hashlib.sha256(value).hexdigest()


def _key_domain() -> dict[str, Any]:
    return {
        "card_count": CARD_COUNT,
        "card_id": {
            "encoding": "rank-major: card_id = rank_index * 4 + suit_index",
            "maximum": 51,
            "minimum": 0,
        },
        "pair_key": {
            "canonicalization": "ascending distinct card ids (card_a < card_b)",
            "domain": "all unordered two-card combinations C(52,2)",
            "index_formula": "card_a * (103 - card_a) // 2 + card_b - card_a - 1",
            "order": "lexicographic by (card_a, card_b)",
        },
        "rank_index": {"maximum": 12, "minimum": 0, "order": "2 through Ace"},
        "suit_index": {
            "maximum": 3,
            "minimum": 0,
            "semantics": "four abstract suits; permutation-invariant metadata",
        },
    }


def _semantics() -> dict[str, Any]:
    return {
        "class_count": CLASS_COUNT,
        "class_id": "class_row * 13 + class_col",
        "class_matrix": {
            "diagonal": "pairs",
            "offsuit": "row=low_rank, column=high_rank",
            "suited": "row=high_rank, column=low_rank",
        },
        "class_multiplicity": {"offsuit": 12, "pair": 6, "suited": 4},
        "flags": {"bit_0": "suited", "bit_1": "pair"},
        "scope": (
            "pure hole-card identity metadata only; no equity, opponent range, "
            "action, or policy values"
        ),
    }


def _record_layout() -> dict[str, Any]:
    return {
        "byte_order": "little-endian",
        "fields": [
            "card_a:u8",
            "card_b:u8",
            "rank_a:u8",
            "rank_b:u8",
            "suit_a:u8",
            "suit_b:u8",
            "high_rank:u8",
            "low_rank:u8",
            "class_id:u16",
            "class_row:u8",
            "class_col:u8",
            "flags:u8",
            "class_combo_count:u8",
        ],
        "record_size": RECORD_SIZE,
    }


def _build_contract(*, generator_commit: str, payload_sha256: str) -> dict[str, Any]:
    return {
        "asset_id": ASSET_ID,
        "binary_format_version": BINARY_FORMAT_VERSION,
        "consumer_contract": {
            "access": "read-only mmap with one fixed-width unpack per lookup",
            "api": "open_hole_combo_metadata_asset(...).lookup(card_a, card_b)",
            "auto_generate_on_read": False,
            "complexity": "O(1) pair-to-record index",
        },
        "generator": {
            "git_commit": generator_commit,
            "module": "web/core/poker_assets.py",
        },
        "key_domain": _key_domain(),
        "ownership": {
            "owner": "evolution_system",
            "write_policy": "offline infrastructure generator only; LLM workers are read-only",
        },
        "payload": {
            "offset": HEADER_SIZE,
            "record_count": RECORD_COUNT,
            "sha256": payload_sha256,
            "size": PAYLOAD_SIZE,
        },
        "record_layout": _record_layout(),
        "schema_version": SCHEMA_VERSION,
        "semantics": _semantics(),
    }


def _require_hex(value: Any, regex: re.Pattern[str], label: str) -> str:
    text = str(value)
    if regex.fullmatch(text) is None:
        raise AssetSchemaError(f"{label} must be lowercase hexadecimal, got {value!r}")
    return text


def _canonical_cards(card_a: int, card_b: int) -> tuple[int, int]:
    for label, card in (("card_a", card_a), ("card_b", card_b)):
        if isinstance(card, bool) or not isinstance(card, int):
            raise ValueError(f"{label} must be an integer card id")
        if not 0 <= card < CARD_COUNT:
            raise ValueError(f"{label} must be in [0, 51], got {card}")
    if card_a == card_b:
        raise ValueError("hole cards must be distinct")
    return (card_a, card_b) if card_a < card_b else (card_b, card_a)


def hole_combo_index(card_a: int, card_b: int) -> int:
    """Return the O(1) lexicographic record index for two distinct card ids."""
    first, second = _canonical_cards(card_a, card_b)
    return first * (103 - first) // 2 + second - first - 1


def _record_values(index: int, card_a: int, card_b: int) -> tuple[int, ...]:
    rank_a, suit_a = divmod(card_a, SUIT_COUNT)
    rank_b, suit_b = divmod(card_b, SUIT_COUNT)
    high_rank = max(rank_a, rank_b)
    low_rank = min(rank_a, rank_b)
    pair = rank_a == rank_b
    suited = not pair and suit_a == suit_b
    if pair or suited:
        class_row, class_col = high_rank, low_rank
    else:
        class_row, class_col = low_rank, high_rank
    class_id = class_row * RANK_COUNT + class_col
    flags = (_FLAG_SUITED if suited else 0) | (_FLAG_PAIR if pair else 0)
    class_combo_count = 6 if pair else (4 if suited else 12)
    return (
        card_a,
        card_b,
        rank_a,
        rank_b,
        suit_a,
        suit_b,
        high_rank,
        low_rank,
        class_id,
        class_row,
        class_col,
        flags,
        class_combo_count,
    )


def _generate_payload() -> bytes:
    payload = bytearray(PAYLOAD_SIZE)
    index = 0
    for card_a in range(CARD_COUNT):
        for card_b in range(card_a + 1, CARD_COUNT):
            expected_index = hole_combo_index(card_a, card_b)
            if expected_index != index:
                raise AssertionError(
                    f"hole-combo ordering drift: {card_a},{card_b} -> {expected_index}, expected {index}"
                )
            _RECORD.pack_into(payload, index * RECORD_SIZE, *_record_values(index, card_a, card_b))
            index += 1
    if index != RECORD_COUNT:
        raise AssertionError(f"record count drift: {index} != {RECORD_COUNT}")
    return bytes(payload)


def _repository_candidate_root() -> Path:
    return Path(__file__).resolve().parents[2] / "bots"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_build_destination(asset_root: Path) -> Path:
    root = asset_root.expanduser().resolve()
    candidate_root = _repository_candidate_root().resolve()
    if root == candidate_root or _is_relative_to(root, candidate_root):
        raise AssetGenerationError(
            "system assets cannot be generated inside bots/; candidate and LLM-owned "
            "artifacts are consumers only"
        )
    return root


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(directory: Path, prefix: str, content: bytes, mode: int) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _publish_manifest(asset_root: Path, content: bytes) -> Path:
    """Publish the manifest pointer last; isolated for failure-injection tests."""
    temp_path = _write_temp(asset_root, ".manifest-", content, 0o444)
    manifest_path = asset_root / MANIFEST_FILENAME
    try:
        os.replace(temp_path, manifest_path)
        _fsync_directory(asset_root)
    finally:
        temp_path.unlink(missing_ok=True)
    return manifest_path


@contextmanager
def _generation_lock(asset_root: Path) -> Iterator[None]:
    lock_path = asset_root / ".asset-generation.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_regular_file(path: Path, *, maximum_bytes: int, require_read_only: bool) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AssetIntegrityError(f"cannot open asset file {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AssetIntegrityError(f"asset path is not a regular file: {path}")
        if require_read_only and info.st_mode & 0o222:
            raise AssetIntegrityError(f"published asset must be read-only: {path}")
        if info.st_size > maximum_bytes:
            raise AssetIntegrityError(
                f"asset file exceeds size boundary: {path} is {info.st_size}, max {maximum_bytes}"
            )
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise AssetIntegrityError(f"short read from asset file: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), info
    finally:
        os.close(fd)


def _publish_blob(asset_root: Path, filename: str, content: bytes, digest: str) -> Path:
    artifact_path = asset_root / filename
    if artifact_path.exists():
        existing, info = _read_regular_file(
            artifact_path,
            maximum_bytes=MAX_ARTIFACT_BYTES,
            require_read_only=True,
        )
        if info.st_size != len(content) or _sha256_bytes(existing) != digest or existing != content:
            raise AssetIntegrityError(
                f"content-addressed artifact already exists with different bytes: {artifact_path}"
            )
        return artifact_path

    temp_path = _write_temp(asset_root, ".artifact-", content, 0o444)
    try:
        os.replace(temp_path, artifact_path)
        _fsync_directory(asset_root)
    finally:
        temp_path.unlink(missing_ok=True)
    return artifact_path


def build_system_hole_combo_metadata_asset(
    asset_root: str | os.PathLike[str],
    *,
    generator_commit: str,
) -> BuiltPokerAsset:
    """Atomically build the immutable 1,326-combination metadata asset.

    This is an offline infrastructure API.  It intentionally requires an
    explicit 40-hex generator commit and refuses any destination below
    ``bots/``.  Normal consumers call :func:`open_hole_combo_metadata_asset`,
    which has no write or auto-generation path.
    """
    generator_commit = _require_hex(generator_commit, _HEX_40, "generator_commit")
    root = _validate_build_destination(Path(asset_root))
    root.mkdir(parents=True, exist_ok=True, mode=0o755)

    payload = _generate_payload()
    payload_sha256 = _sha256_bytes(payload)
    contract = _build_contract(
        generator_commit=generator_commit,
        payload_sha256=payload_sha256,
    )
    contract_sha256 = _sha256_bytes(_canonical_json(contract))
    header = _HEADER.pack(
        _MAGIC,
        BINARY_FORMAT_VERSION,
        SCHEMA_VERSION,
        HEADER_SIZE,
        RECORD_SIZE,
        RECORD_COUNT,
        HEADER_SIZE,
        PAYLOAD_SIZE,
        CLASS_COUNT,
        bytes.fromhex(contract_sha256),
        bytes.fromhex(payload_sha256),
        generator_commit.encode("ascii"),
    )
    artifact = header + payload
    if len(artifact) != ARTIFACT_SIZE or len(artifact) > MAX_ARTIFACT_BYTES:
        raise AssertionError(f"artifact size drift: {len(artifact)}")
    artifact_sha256 = _sha256_bytes(artifact)
    artifact_filename = f"{ARTIFACT_PREFIX}-{artifact_sha256}.bin"
    manifest = {
        "artifact": {
            "file": artifact_filename,
            "sha256": artifact_sha256,
            "size": len(artifact),
        },
        "contract": contract,
        "contract_sha256": contract_sha256,
        "manifest_format": MANIFEST_FORMAT,
    }
    manifest_content = _canonical_json(manifest) + b"\n"
    if len(manifest_content) > MAX_MANIFEST_BYTES:
        raise AssertionError(f"manifest size drift: {len(manifest_content)}")

    with _generation_lock(root):
        artifact_path = _publish_blob(
            root,
            artifact_filename,
            artifact,
            artifact_sha256,
        )
        manifest_path = _publish_manifest(root, manifest_content)

    return BuiltPokerAsset(
        manifest_path=manifest_path,
        artifact_path=artifact_path,
        contract_sha256=contract_sha256,
        payload_sha256=payload_sha256,
        artifact_sha256=artifact_sha256,
        generator_commit=generator_commit,
    )


class HoleComboMetadataAsset:
    """Read-only mmap consumer for the frozen hole-combination schema."""

    def __init__(
        self,
        *,
        mapping: mmap.mmap,
        manifest: Mapping[str, Any],
        manifest_bytes: int,
        artifact_path: Path,
    ) -> None:
        self._mapping: mmap.mmap | None = mapping
        self._manifest = manifest
        self._artifact_path = artifact_path
        self._decoded_records = 0
        self.startup_stats = AssetStartupStats(
            manifest_bytes=manifest_bytes,
            mapped_bytes=len(mapping),
            eager_records_decoded=0,
        )

    @property
    def artifact_path(self) -> Path:
        return self._artifact_path

    @property
    def artifact_sha256(self) -> str:
        return str(self._manifest["artifact"]["sha256"])

    @property
    def payload_sha256(self) -> str:
        return str(self._manifest["contract"]["payload"]["sha256"])

    @property
    def generator_commit(self) -> str:
        return str(self._manifest["contract"]["generator"]["git_commit"])

    @property
    def decoded_records(self) -> int:
        return self._decoded_records

    @property
    def closed(self) -> bool:
        return self._mapping is None

    def __len__(self) -> int:
        return RECORD_COUNT

    def _require_open(self) -> mmap.mmap:
        if self._mapping is None:
            raise PokerAssetError("hole-combo metadata asset is closed")
        return self._mapping

    def lookup_index(self, index: int) -> HoleComboMetadata:
        """Decode exactly one record by its stable lexicographic index."""
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("record index must be an integer")
        if not 0 <= index < RECORD_COUNT:
            raise IndexError(f"record index must be in [0, {RECORD_COUNT - 1}]")
        mapping = self._require_open()
        values = _RECORD.unpack_from(mapping, HEADER_SIZE + index * RECORD_SIZE)
        (
            card_a,
            card_b,
            rank_a,
            rank_b,
            suit_a,
            suit_b,
            high_rank,
            low_rank,
            class_id,
            class_row,
            class_col,
            flags,
            class_combo_count,
        ) = values
        self._decoded_records += 1
        return HoleComboMetadata(
            index=index,
            card_a=card_a,
            card_b=card_b,
            rank_a=rank_a,
            rank_b=rank_b,
            suit_a=suit_a,
            suit_b=suit_b,
            high_rank=high_rank,
            low_rank=low_rank,
            class_id=class_id,
            class_row=class_row,
            class_col=class_col,
            suited=bool(flags & _FLAG_SUITED),
            pair=bool(flags & _FLAG_PAIR),
            class_combo_count=class_combo_count,
        )

    def lookup(self, card_a: int, card_b: int) -> HoleComboMetadata:
        """Return metadata in O(1), accepting either order of two card ids."""
        first, second = _canonical_cards(card_a, card_b)
        record = self.lookup_index(hole_combo_index(first, second))
        if (record.card_a, record.card_b) != (first, second):
            raise AssetIntegrityError(
                "record key does not match its O(1) index; asset payload is inconsistent"
            )
        return record

    def iter_records(self) -> Iterator[HoleComboMetadata]:
        for index in range(RECORD_COUNT):
            yield self.lookup_index(index)

    def close(self) -> None:
        mapping, self._mapping = self._mapping, None
        if mapping is not None:
            mapping.close()

    def __enter__(self) -> "HoleComboMetadataAsset":
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _validate_manifest(manifest: Any) -> tuple[dict[str, Any], str, str, int]:
    if not isinstance(manifest, dict) or set(manifest) != {
        "artifact",
        "contract",
        "contract_sha256",
        "manifest_format",
    }:
        raise AssetSchemaError("manifest top-level schema mismatch")
    if manifest["manifest_format"] != MANIFEST_FORMAT:
        raise AssetSchemaError(f"unsupported manifest format: {manifest['manifest_format']!r}")

    artifact = manifest["artifact"]
    contract = manifest["contract"]
    if not isinstance(artifact, dict) or set(artifact) != {"file", "sha256", "size"}:
        raise AssetSchemaError("manifest artifact schema mismatch")
    if not isinstance(contract, dict):
        raise AssetSchemaError("manifest contract must be an object")

    generator = contract.get("generator")
    payload = contract.get("payload")
    if not isinstance(generator, dict) or not isinstance(payload, dict):
        raise AssetSchemaError("manifest generator/payload contract missing")
    generator_commit = _require_hex(generator.get("git_commit"), _HEX_40, "generator_commit")
    payload_sha256 = _require_hex(payload.get("sha256"), _HEX_64, "payload_sha256")
    expected_contract = _build_contract(
        generator_commit=generator_commit,
        payload_sha256=payload_sha256,
    )
    if contract != expected_contract:
        raise AssetSchemaError("manifest contract differs from the frozen schema/semantics")
    contract_sha256 = _require_hex(
        manifest["contract_sha256"],
        _HEX_64,
        "contract_sha256",
    )
    if _sha256_bytes(_canonical_json(contract)) != contract_sha256:
        raise AssetIntegrityError("manifest contract SHA-256 mismatch")

    artifact_sha256 = _require_hex(artifact.get("sha256"), _HEX_64, "artifact_sha256")
    filename = artifact.get("file")
    expected_filename = f"{ARTIFACT_PREFIX}-{artifact_sha256}.bin"
    if filename != expected_filename or Path(str(filename)).name != filename:
        raise AssetIntegrityError("artifact filename is not content-addressed or is unsafe")
    size = artifact.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size != ARTIFACT_SIZE:
        raise AssetSchemaError(f"artifact size must be exactly {ARTIFACT_SIZE}")
    return contract, contract_sha256, artifact_sha256, size


def open_hole_combo_metadata_asset(
    asset_root: str | os.PathLike[str],
) -> HoleComboMetadataAsset:
    """Open and fully verify the published asset as a read-only mmap.

    The manifest is an atomic pointer to an immutable content-addressed blob.
    This function never creates a directory, lock, manifest, or artifact.
    """
    root = Path(asset_root).expanduser().resolve()
    manifest_path = root / MANIFEST_FILENAME
    raw_manifest, manifest_info = _read_regular_file(
        manifest_path,
        maximum_bytes=MAX_MANIFEST_BYTES,
        require_read_only=True,
    )
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssetSchemaError(f"invalid UTF-8/JSON manifest: {exc}") from exc
    if raw_manifest != _canonical_json(manifest) + b"\n":
        raise AssetSchemaError("manifest must use canonical JSON encoding")
    contract, contract_sha256, artifact_sha256, artifact_size = _validate_manifest(manifest)

    artifact_path = root / str(manifest["artifact"]["file"])
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(artifact_path, flags)
    except OSError as exc:
        raise AssetIntegrityError(f"cannot open immutable artifact {artifact_path}: {exc}") from exc
    mapping: mmap.mmap | None = None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AssetIntegrityError("artifact is not a regular file")
        if info.st_mode & 0o222:
            raise AssetIntegrityError("published artifact must be read-only")
        if info.st_size != artifact_size or info.st_size > MAX_ARTIFACT_BYTES:
            raise AssetIntegrityError(
                f"artifact size mismatch: {info.st_size} != {artifact_size}"
            )
        mapping = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
    finally:
        os.close(fd)

    try:
        if _sha256_bytes(mapping) != artifact_sha256:
            raise AssetIntegrityError("artifact SHA-256 mismatch")
        header = _HEADER.unpack_from(mapping, 0)
        (
            magic,
            binary_version,
            schema_version,
            header_size,
            record_size,
            record_count,
            payload_offset,
            payload_size,
            class_count,
            header_contract_digest,
            header_payload_digest,
            header_generator_commit,
        ) = header
        expected_header_scalars = (
            _MAGIC,
            BINARY_FORMAT_VERSION,
            SCHEMA_VERSION,
            HEADER_SIZE,
            RECORD_SIZE,
            RECORD_COUNT,
            HEADER_SIZE,
            PAYLOAD_SIZE,
            CLASS_COUNT,
        )
        if header[:9] != expected_header_scalars:
            raise AssetSchemaError("binary header/schema/count boundary mismatch")
        if header_contract_digest.hex() != contract_sha256:
            raise AssetIntegrityError("binary header contract digest mismatch")
        payload_sha256 = str(contract["payload"]["sha256"])
        if header_payload_digest.hex() != payload_sha256:
            raise AssetIntegrityError("binary header payload digest mismatch")
        try:
            decoded_commit = header_generator_commit.decode("ascii")
        except UnicodeDecodeError as exc:
            raise AssetSchemaError("binary generator commit is not ASCII") from exc
        if decoded_commit != contract["generator"]["git_commit"]:
            raise AssetIntegrityError("binary generator commit mismatch")
        payload_view = memoryview(mapping)[payload_offset : payload_offset + payload_size]
        try:
            actual_payload_sha256 = _sha256_bytes(payload_view)
        finally:
            payload_view.release()
        if actual_payload_sha256 != payload_sha256:
            raise AssetIntegrityError("binary payload SHA-256 mismatch")
        return HoleComboMetadataAsset(
            mapping=mapping,
            manifest=manifest,
            manifest_bytes=manifest_info.st_size,
            artifact_path=artifact_path,
        )
    except BaseException:
        mapping.close()
        raise


assert HEADER_SIZE == 160
assert RECORD_SIZE == 14
assert RECORD_COUNT == CARD_COUNT * (CARD_COUNT - 1) // 2
assert CLASS_COUNT == RANK_COUNT * RANK_COUNT
assert ARTIFACT_SIZE <= MAX_ARTIFACT_BYTES
