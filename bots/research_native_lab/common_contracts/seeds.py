"""Deterministic, non-overlapping seed partitions with held-out commitments."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .constants import CONTRACT_VERSION


DEVELOPMENT_SPLITS = ("train", "dev", "validation")

DRAND_CHAIN_HASH = "8990e7a9aaed2ffed73dbd7092123d6f289930540d7651336225dc172e51b2ce"
DRAND_PUBLIC_KEY = (
    "868f005eb8e6e4ca0a47c8a77ceaa5309a47978a7c71bc5cce96366b5d7a569"
    "937c529eeda66c7293784a9402801af31"
)
DRAND_GENESIS_TIME = 1_595_431_050
DRAND_GENESIS_SEED = "176f93498eac9ca337150b46d21dd58673ea4e3581185f869672e59fa4cb390a"
DRAND_PERIOD_SEC = 30
FINAL_BEACON_DELAY_SEC = 3_600
FUTURE_BITCOIN_BLOCK_OFFSET = 12
DRAND_ENDPOINTS = (
    "https://api.drand.sh/v2/",
    "https://api2.drand.sh/v2/",
    "https://api3.drand.sh/v2/",
)
DRAND_SCHEME = "pedersen-bls-chained"
DRAND_BEACON_ID = "default"
FREEZE_MINIMUM_CONFIRMATIONS = 6
FORMAL_DECK_ROOT_POOL_SIZE = 8_192
FORMAL_FUTURE_ENTROPY_AVAILABLE = False
FORMAL_FUTURE_ENTROPY_UNAVAILABLE_REASON = (
    "formal future entropy is unavailable: the installed path has no fixed root-owned "
    "synced-mainnet/chainwork authority, no independently signed freeze observation, "
    "and no verified future-block-plus-drand entropy mix; caller-selected loopback RPC "
    "and a drand round derived from miner header time are diagnostic only"
)
FREEZE_AUTHORITY_DIAGNOSTIC = "caller-rpc-ots-diagnostic-only"
FREEZE_AUTHORITY_EXTERNAL = "fixed-independent-mainnet-freeze-authority-v1"
FORMAL_DECK_NAMESPACE_TEMPLATE = "formal/{seed_cohort_digest}/deck-root"
FORMAL_POLICY_NAMESPACE_TEMPLATE = (
    "formal/{seed_cohort_digest}/policy/{artifact_identity_digest}"
)
FORMAL_ANALYSIS_NAMESPACE_TEMPLATE = (
    "formal/{seed_cohort_digest}/analysis/{analysis_domain}/{hypothesis_digest}"
)
_TOOLS_DIR = Path(__file__).resolve().parent / "tools"
_DRAND_LOCK_PATH = _TOOLS_DIR / "verify_drand_beacon.lock.json"
_FREEZE_LOCK_PATH = _TOOLS_DIR / "verify_candidate_freeze.lock.json"
# These digests are filled from the reviewed lock files, not from caller data.
DRAND_VERIFIER_LOCK_SHA256 = "c9d98582aa74057b5af8790ed5a86e036790cdf4e1d5b3a6d1368f81d4e6093a"
FREEZE_VERIFIER_LOCK_SHA256 = "41e022e39c388d2415fe0ba2833e19d62442cf0180c48604dfafe3f40803bd43"
_MAX_JSON_BYTES = 1_048_576
_MAX_PROOF_BYTES = 16_777_216
_VERIFIED_BEACON_TOKEN = object()
_VERIFIED_FREEZE_TOKEN = object()
_FINAL_PLAN_TOKEN = object()


@dataclass(frozen=True, slots=True)
class FormalSeedCohort:
    """Candidate-neutral common-random-number cohort.

    A cohort is deliberately unable to name a focal candidate, training
    checkpoint, or comparison mode.  Those dimensions may change the policy
    artifact, but they must not change the 70-deal sequence used for a paired
    comparison.  ``counterparty_scope_digest`` can bind an external opponent,
    an opaque held-out slot, or a canonical unordered route pair.
    """

    comparison_domain: str
    budget_ms: int
    counterparty_scope_digest: str
    paired_block_count: int
    common_contract_digest: str

    def __post_init__(self) -> None:
        if self.comparison_domain not in {
            "direct-h2h",
            "external-opponent",
            "ablation",
        }:
            raise ValueError("unknown formal seed cohort comparison domain")
        if type(self.budget_ms) is not int or self.budget_ms <= 0:
            raise ValueError("formal seed cohort budget must be a positive integer")
        if type(self.paired_block_count) is not int or self.paired_block_count <= 0:
            raise ValueError("formal seed cohort block count must be a positive integer")
        object.__setattr__(
            self,
            "counterparty_scope_digest",
            _require_digest(
                self.counterparty_scope_digest,
                "seed cohort counterparty scope digest",
            ),
        )
        object.__setattr__(
            self,
            "common_contract_digest",
            _require_digest(self.common_contract_digest, "seed cohort common contract digest"),
        )

    def digest(self) -> str:
        payload = {
            "comparison_domain": self.comparison_domain,
            "budget_ms": self.budget_ms,
            "counterparty_scope_digest": self.counterparty_scope_digest,
            "paired_block_count": self.paired_block_count,
            "common_contract_digest": self.common_contract_digest,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def _commit(values: tuple[int, ...]) -> str:
    payload = json.dumps(list(values), separators=(",", ":")).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def derive_child_seed(root_seed: int, namespace: str, index: int) -> int:
    """Expand a frozen root without sharing streams across semantic consumers."""

    if not namespace or "\x00" in namespace:
        raise ValueError("seed namespace must be non-empty and NUL-free")
    if not isinstance(index, int) or index < 0:
        raise ValueError("seed child index must be a nonnegative integer")
    digest = hashlib.sha256(
        f"{CONTRACT_VERSION}|child|{root_seed}|{namespace}|{index}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


@dataclass(frozen=True, slots=True)
class SeedPartition:
    master_commitment: str
    splits: Mapping[str, tuple[int, ...]]

    @classmethod
    def freeze(cls, master_seed: int, counts: Mapping[str, int]) -> "SeedPartition":
        if set(counts) != set(DEVELOPMENT_SPLITS):
            raise ValueError(f"seed counts must contain exactly {DEVELOPMENT_SPLITS}")
        if any(not isinstance(count, int) or count <= 0 for count in counts.values()):
            raise ValueError("all seed counts must be positive integers")
        used: set[int] = set()
        splits: dict[str, tuple[int, ...]] = {}
        for namespace in DEVELOPMENT_SPLITS:
            values: list[int] = []
            counter = 0
            while len(values) < counts[namespace]:
                digest = hashlib.sha256(
                    f"{CONTRACT_VERSION}|{master_seed}|{namespace}|{counter}".encode("utf-8")
                ).digest()
                value = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
                counter += 1
                if value in used:
                    continue
                used.add(value)
                values.append(value)
            splits[namespace] = tuple(values)
        master_commitment = hashlib.sha256(
            f"{CONTRACT_VERSION}|master|{master_seed}".encode("utf-8")
        ).hexdigest()
        return cls(master_commitment=master_commitment, splits=splits)

    def assert_disjoint(self) -> None:
        seen: set[int] = set()
        for namespace in DEVELOPMENT_SPLITS:
            values = self.splits[namespace]
            if len(set(values)) != len(values) or seen.intersection(values):
                raise ValueError(f"seed overlap detected in {namespace}")
            seen.update(values)

    def commitments(self) -> dict[str, str]:
        self.assert_disjoint()
        return {namespace: _commit(self.splits[namespace]) for namespace in DEVELOPMENT_SPLITS}

    def public_manifest(self) -> dict:
        self.assert_disjoint()
        return {
            "contract_version": CONTRACT_VERSION,
            "master_commitment": self.master_commitment,
            "split_commitments": self.commitments(),
            "counts": {name: len(self.splits[name]) for name in DEVELOPMENT_SPLITS},
            "seeds": {
                name: list(self.splits[name])
                for name in DEVELOPMENT_SPLITS
            },
            "scope": "development_only_no_final_heldout_material",
        }

    def verify_manifest(self, manifest: Mapping) -> None:
        if manifest.get("contract_version") != CONTRACT_VERSION:
            raise ValueError("seed manifest contract version mismatch")
        if manifest.get("master_commitment") != self.master_commitment:
            raise ValueError("master seed commitment mismatch")
        if dict(manifest.get("split_commitments", {})) != self.commitments():
            raise ValueError("split commitment mismatch")


def _require_digest(value: str, name: str) -> str:
    try:
        decoded = bytes.fromhex(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be hexadecimal") from exc
    if len(decoded) != 32:
        raise ValueError(f"{name} must be a 32-byte SHA-256-style digest")
    return value.lower()


def formal_deck_seed_namespace(seed_cohort_digest: str) -> str:
    """Return the sole formal deck-root namespace for a candidate-neutral cohort."""

    cohort = _require_digest(seed_cohort_digest, "formal seed cohort digest")
    return FORMAL_DECK_NAMESPACE_TEMPLATE.format(seed_cohort_digest=cohort)


def formal_policy_seed_namespace(
    seed_cohort_digest: str,
    artifact_identity_digest: str,
) -> str:
    """Return the identity-following formal policy RNG namespace."""

    cohort = _require_digest(seed_cohort_digest, "formal seed cohort digest")
    identity = _require_digest(artifact_identity_digest, "artifact identity digest")
    return FORMAL_POLICY_NAMESPACE_TEMPLATE.format(
        seed_cohort_digest=cohort,
        artifact_identity_digest=identity,
    )


def formal_analysis_seed_namespace(
    seed_cohort_digest: str,
    analysis_domain: str,
    hypothesis_digest: str,
) -> str:
    """Return a future-beacon analysis stream with no caller-selected seed."""

    cohort = _require_digest(seed_cohort_digest, "formal seed cohort digest")
    hypothesis = _require_digest(hypothesis_digest, "formal hypothesis digest")
    if analysis_domain not in {"bootstrap", "sign-flip"}:
        raise ValueError("formal analysis domain must be bootstrap or sign-flip")
    return FORMAL_ANALYSIS_NAMESPACE_TEMPLATE.format(
        seed_cohort_digest=cohort,
        analysis_domain=analysis_domain,
        hypothesis_digest=hypothesis,
    )


def _validate_formal_seed_namespace(namespace: str) -> str:
    if not isinstance(namespace, str):
        raise ValueError("formal seed namespace must be a string")
    parts = namespace.split("/")
    if len(parts) == 3 and parts[0] == "formal" and parts[2] == "deck-root":
        canonical = formal_deck_seed_namespace(parts[1])
    elif (
        len(parts) == 4
        and parts[0] == "formal"
        and parts[2] == "policy"
    ):
        canonical = formal_policy_seed_namespace(parts[1], parts[3])
    else:
        raise ValueError("namespace is not a registered formal seed stream")
    if namespace != canonical:
        raise ValueError("formal seed namespace is not canonical")
    return canonical


def _require_hex(value: Any, name: str, byte_length: int) -> str:
    if not isinstance(value, str) or value != value.lower():
        raise ValueError(f"{name} must be lowercase hexadecimal")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be lowercase hexadecimal") from exc
    if len(decoded) != byte_length:
        raise ValueError(f"{name} must be exactly {byte_length} bytes")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_regular_file(path: str | os.PathLike[str], maximum: int) -> bytes:
    target = Path(path)
    metadata = target.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{target} must be a regular non-symlink file")
    if metadata.st_size > maximum:
        raise ValueError(f"{target} exceeds the frozen size bound")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError(f"{target} changed while it was opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(131_072, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError(f"{target} exceeds the frozen size bound")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _strict_json(payload: bytes, name: str) -> Any:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant in {name}: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not strict UTF-8 JSON") from exc


def _require_exact_keys(value: Any, expected: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{name} fields differ from the frozen schema")
    return value


def _load_pinned_lock(path: Path, expected_sha256: str, schema: str) -> dict[str, Any]:
    payload = _read_regular_file(path, _MAX_JSON_BYTES)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError(f"{path.name} differs from its reviewed digest")
    lock = _strict_json(payload, path.name)
    if not isinstance(lock, dict) or lock.get("schema") != schema:
        raise ValueError(f"{path.name} has an unexpected schema")
    return lock


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _resolve_manifest_file(root: Path, value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise ValueError(f"{name} must be a single relative filename")
    target = root / value
    # _read_regular_file performs the no-symlink and inode-stability checks.
    return target


class CandidateFreezeState(str, Enum):
    """Fail-closed lifecycle for the non-rewritable candidate freeze proof."""

    UNSTAMPED = "unstamped"
    PENDING_BITCOIN = "pending_bitcoin"
    VERIFIED_BITCOIN = "verified_bitcoin"
    INVALID = "invalid"
    VERIFIER_ERROR = "verifier_error"


def candidate_freeze_record_bytes(candidate_bundle_digest: str) -> bytes:
    digest = _require_digest(candidate_bundle_digest, "candidate_bundle_digest")
    return _canonical_json(
        {
            "candidate_bundle_digest": digest,
            "schema": "candidate-freeze-record-v1",
        }
    )


def write_candidate_freeze_record(
    path: str | os.PathLike[str], candidate_bundle_digest: str
) -> str:
    """Create, but never overwrite, the exact record an operator must timestamp."""

    payload = candidate_freeze_record_bytes(candidate_bundle_digest)
    target = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateFreezeVerification:
    state: CandidateFreezeState
    reason: str
    candidate_bundle_digest: str
    record_sha256: str
    proof_sha256: str | None = None
    bitcoin_height: int | None = None
    bitcoin_block_hash: str | None = None
    attested_epoch: int | None = None
    confirmations: int | None = None
    receipt_digest: str | None = None
    _token: object = field(default=None, init=False, repr=False, compare=False)

    def require_verified(self) -> "VerifiedCandidateFreeze":
        if self.state is not CandidateFreezeState.VERIFIED_BITCOIN:
            raise ValueError(f"candidate freeze is not Bitcoin-verified: {self.state.value}")
        if self._token is not _VERIFIED_FREEZE_TOKEN:
            raise ValueError("candidate freeze was not created by the formal verifier")
        if (
            self.proof_sha256 is None
            or self.bitcoin_height is None
            or self.bitcoin_block_hash is None
            or self.attested_epoch is None
            or self.confirmations is None
            or self.receipt_digest is None
        ):
            raise ValueError("verified candidate freeze receipt is incomplete")
        freeze = VerifiedCandidateFreeze(
            candidate_bundle_digest=self.candidate_bundle_digest,
            record_sha256=self.record_sha256,
            proof_sha256=self.proof_sha256,
            bitcoin_height=self.bitcoin_height,
            bitcoin_block_hash=self.bitcoin_block_hash,
            attested_epoch=self.attested_epoch,
            confirmations=self.confirmations,
            receipt_digest=self.receipt_digest,
        )
        object.__setattr__(freeze, "_token", _VERIFIED_FREEZE_TOKEN)
        return freeze


@dataclass(frozen=True, slots=True)
class VerifiedCandidateFreeze:
    candidate_bundle_digest: str
    record_sha256: str
    proof_sha256: str
    bitcoin_height: int
    bitcoin_block_hash: str
    attested_epoch: int
    confirmations: int
    receipt_digest: str
    authority_kind: str = FREEZE_AUTHORITY_DIAGNOSTIC
    _token: object = field(default=None, init=False, repr=False, compare=False)
    _formal_authority_guard: object = field(
        default=None, init=False, repr=False, compare=False
    )

    def _assert_for(self, candidate_bundle_digest: str) -> None:
        if self._token is not _VERIFIED_FREEZE_TOKEN:
            raise ValueError("candidate freeze was not created by the formal verifier")
        digest = _require_digest(candidate_bundle_digest, "candidate_bundle_digest")
        if self.candidate_bundle_digest != digest:
            raise ValueError("candidate freeze belongs to a different candidate bundle")
        _require_digest(self.record_sha256, "freeze record digest")
        _require_digest(self.proof_sha256, "freeze proof digest")
        _require_digest(self.bitcoin_block_hash, "Bitcoin block hash")
        _require_digest(self.receipt_digest, "freeze verifier receipt digest")
        if type(self.bitcoin_height) is not int or self.bitcoin_height < 0:
            raise ValueError("invalid Bitcoin attestation height")
        if type(self.attested_epoch) is not int or self.attested_epoch < DRAND_GENESIS_TIME:
            raise ValueError("invalid Bitcoin attestation epoch")
        if type(self.confirmations) is not int or self.confirmations < FREEZE_MINIMUM_CONFIRMATIONS:
            raise ValueError("candidate freeze lacks the required Bitcoin confirmations")
        if self.authority_kind not in {
            FREEZE_AUTHORITY_DIAGNOSTIC,
            FREEZE_AUTHORITY_EXTERNAL,
        }:
            raise ValueError("candidate freeze has an unknown authority kind")

    def _assert_formal_authority(self, candidate_bundle_digest: str) -> None:
        """Require a fixed independent authority, never the caller's RPC verdict."""

        self._assert_for(candidate_bundle_digest)
        guard = self._formal_authority_guard
        if (
            not FORMAL_FUTURE_ENTROPY_AVAILABLE
            or self.authority_kind != FREEZE_AUTHORITY_EXTERNAL
            or not callable(guard)
            or guard(self) is not True
        ):
            raise ValueError(FORMAL_FUTURE_ENTROPY_UNAVAILABLE_REASON)


def _freeze_result(
    *,
    state: CandidateFreezeState,
    reason: str,
    candidate_bundle_digest: str,
    record_sha256: str,
    proof_sha256: str | None = None,
    tool_result: Mapping[str, Any] | None = None,
    verifier_lock: Mapping[str, Any] | None = None,
) -> CandidateFreezeVerification:
    if state is not CandidateFreezeState.VERIFIED_BITCOIN:
        return CandidateFreezeVerification(
            state=state,
            reason=reason,
            candidate_bundle_digest=candidate_bundle_digest,
            record_sha256=record_sha256,
            proof_sha256=proof_sha256,
        )
    if tool_result is None or verifier_lock is None:
        raise ValueError("verified freeze result lacks formal verifier evidence")
    bitcoin = _require_exact_keys(
        tool_result.get("bitcoin"),
        {
            "network",
            "height",
            "block_hash",
            "attested_epoch",
            "confirmations",
            "best_height",
        },
        "Bitcoin receipt",
    )
    height = bitcoin["height"]
    attested_epoch = bitcoin["attested_epoch"]
    confirmations = bitcoin["confirmations"]
    best_height = bitcoin["best_height"]
    if (
        bitcoin["network"] != "main"
        or type(height) is not int
        or height < 0
        or type(attested_epoch) is not int
        or attested_epoch < DRAND_GENESIS_TIME
        or type(confirmations) is not int
        or confirmations < FREEZE_MINIMUM_CONFIRMATIONS
        or type(best_height) is not int
        or best_height < height
    ):
        raise ValueError("Bitcoin receipt integer fields are invalid")
    block_hash = _require_hex(bitcoin["block_hash"], "Bitcoin block hash", 32)
    receipt = {
        "candidate_bundle_digest": candidate_bundle_digest,
        "freeze_verifier_lock_sha256": FREEZE_VERIFIER_LOCK_SHA256,
        "record_sha256": record_sha256,
        "proof_sha256": proof_sha256,
        "tool_result": dict(tool_result),
        "verifier_adapter_sha256": verifier_lock["adapter"]["sha256"],
        "verifier_requirements_sha256": verifier_lock["requirements"]["sha256"],
    }
    receipt_digest = hashlib.sha256(_canonical_json(receipt)).hexdigest()
    verification = CandidateFreezeVerification(
        state=state,
        reason=reason,
        candidate_bundle_digest=candidate_bundle_digest,
        record_sha256=record_sha256,
        proof_sha256=proof_sha256,
        bitcoin_height=height,
        bitcoin_block_hash=block_hash,
        attested_epoch=attested_epoch,
        confirmations=confirmations,
        receipt_digest=receipt_digest,
    )
    object.__setattr__(verification, "_token", _VERIFIED_FREEZE_TOKEN)
    return verification


def _run_candidate_freeze_verifier(command: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """Private test seam; production callers cannot supply a verification verdict."""

    return subprocess.run(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=600,
        check=False,
        env={
            "HOME": os.devnull,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONHASHSEED": "0",
        },
    )


def verify_candidate_freeze(
    candidate_bundle_digest: str,
    record_path: str | os.PathLike[str],
    proof_path: str | os.PathLike[str],
    *,
    wheelhouse: str | os.PathLike[str],
    bitcoin_rpc_url: str,
) -> CandidateFreezeVerification:
    """Run the locked OTS diagnostic against a caller-selected Bitcoin RPC.

    This rejects caller-provided booleans and raw epochs, but the RPC endpoint
    is still caller owned.  Consequently the returned token supports
    deterministic diagnostics only and cannot satisfy ``_assert_formal``.
    """

    candidate_digest = _require_digest(candidate_bundle_digest, "candidate_bundle_digest")
    record_payload = _read_regular_file(record_path, _MAX_JSON_BYTES)
    if record_payload != candidate_freeze_record_bytes(candidate_digest):
        raise ValueError("candidate freeze record is not the canonical candidate-bound record")
    record_sha256 = hashlib.sha256(record_payload).hexdigest()
    proof = Path(proof_path)
    try:
        proof_payload = _read_regular_file(proof, _MAX_PROOF_BYTES)
    except FileNotFoundError:
        return _freeze_result(
            state=CandidateFreezeState.UNSTAMPED,
            reason="timestamp_file_missing",
            candidate_bundle_digest=candidate_digest,
            record_sha256=record_sha256,
        )
    proof_sha256 = hashlib.sha256(proof_payload).hexdigest()

    lock = _load_pinned_lock(
        _FREEZE_LOCK_PATH,
        FREEZE_VERIFIER_LOCK_SHA256,
        "candidate-freeze-verifier-lock-v1",
    )
    adapter_name = lock["adapter"]["path"]
    requirements_name = lock["requirements"]["path"]
    adapter = _resolve_manifest_file(_TOOLS_DIR, adapter_name, "freeze verifier adapter")
    requirements = _resolve_manifest_file(
        _TOOLS_DIR, requirements_name, "freeze verifier requirements"
    )
    if hashlib.sha256(_read_regular_file(adapter, _MAX_JSON_BYTES)).hexdigest() != lock[
        "adapter"
    ]["sha256"]:
        raise ValueError("candidate freeze verifier adapter digest mismatch")
    if hashlib.sha256(_read_regular_file(requirements, _MAX_JSON_BYTES)).hexdigest() != lock[
        "requirements"
    ]["sha256"]:
        raise ValueError("candidate freeze verifier requirements digest mismatch")

    completed = _run_candidate_freeze_verifier(
        (
            sys.executable,
            str(adapter),
            "--record",
            str(Path(record_path).resolve()),
            "--proof",
            str(proof.resolve()),
            "--wheelhouse",
            str(Path(wheelhouse).resolve()),
            "--bitcoin-node",
            bitcoin_rpc_url,
            "--minimum-confirmations",
            str(FREEZE_MINIMUM_CONFIRMATIONS),
        )
    )
    if completed.returncode != 0 or completed.stderr:
        return _freeze_result(
            state=CandidateFreezeState.VERIFIER_ERROR,
            reason="formal_verifier_process_failed",
            candidate_bundle_digest=candidate_digest,
            record_sha256=record_sha256,
            proof_sha256=proof_sha256,
        )
    if len(completed.stdout) > _MAX_JSON_BYTES:
        raise ValueError("candidate freeze verifier output exceeds size bound")
    tool_result = _strict_json(completed.stdout, "candidate freeze verifier output")
    if completed.stdout != _canonical_json(tool_result):
        raise ValueError("candidate freeze verifier output is not canonical single-record JSON")
    if not isinstance(tool_result, dict) or tool_result.get("schema") != (
        "candidate-freeze-verification-result-v1"
    ):
        raise ValueError("candidate freeze verifier returned an unexpected schema")
    allowed_fields = {
        "schema",
        "state",
        "reason",
        "record_sha256",
        "proof_sha256",
        "minimum_confirmations",
        "bitcoin",
    }
    if not set(tool_result).issubset(allowed_fields):
        raise ValueError("candidate freeze verifier returned unknown fields")
    try:
        state = CandidateFreezeState(tool_result.get("state"))
    except ValueError as exc:
        raise ValueError("candidate freeze verifier returned an unknown state") from exc
    if state is CandidateFreezeState.UNSTAMPED:
        raise ValueError("an existing proof cannot produce the unstamped state")
    reason = tool_result.get("reason")
    if not isinstance(reason, str) or not reason:
        raise ValueError("candidate freeze verifier reason is missing")
    if state is CandidateFreezeState.VERIFIER_ERROR and "record_sha256" not in tool_result:
        return _freeze_result(
            state=state,
            reason=reason,
            candidate_bundle_digest=candidate_digest,
            record_sha256=record_sha256,
            proof_sha256=proof_sha256,
        )
    if tool_result.get("record_sha256") != record_sha256:
        raise ValueError("candidate freeze verifier did not bind the saved record")
    if tool_result.get("proof_sha256") != proof_sha256:
        raise ValueError("candidate freeze verifier did not bind the saved proof")
    if tool_result.get("minimum_confirmations") != FREEZE_MINIMUM_CONFIRMATIONS:
        raise ValueError("candidate freeze verifier changed the confirmation policy")
    return _freeze_result(
        state=state,
        reason=reason,
        candidate_bundle_digest=candidate_digest,
        record_sha256=record_sha256,
        proof_sha256=proof_sha256,
        tool_result=tool_result,
        verifier_lock=lock,
    )


@dataclass(frozen=True, slots=True)
class _ParsedBeacon:
    round: int
    randomness: str
    signature: str
    previous_signature: str

    def to_official_dict(self) -> dict[str, Any]:
        return {
            "previous_signature": self.previous_signature,
            "randomness": self.randomness,
            "round": self.round,
            "signature": self.signature,
        }


def _parse_chain_info(payload: bytes, name: str) -> dict[str, Any]:
    info = _require_exact_keys(
        _strict_json(payload, name),
        {
            "public_key",
            "period",
            "genesis_time",
            "genesis_seed",
            "chain_hash",
            "scheme",
            "beacon_id",
        },
        name,
    )
    if (
        _require_hex(info["public_key"], f"{name} public key", 48) != DRAND_PUBLIC_KEY
        or type(info["period"]) is not int
        or info["period"] != DRAND_PERIOD_SEC
        or type(info["genesis_time"]) is not int
        or info["genesis_time"] != DRAND_GENESIS_TIME
        or _require_hex(info["genesis_seed"], f"{name} genesis seed", 32)
        != DRAND_GENESIS_SEED
        or _require_hex(info["chain_hash"], f"{name} chain hash", 32) != DRAND_CHAIN_HASH
        or info["scheme"] != DRAND_SCHEME
        or info["beacon_id"] != DRAND_BEACON_ID
    ):
        raise ValueError("drand chain info differs from the frozen chain")
    return info


def _parse_beacon(payload: bytes, expected_round: int, name: str) -> _ParsedBeacon:
    value = _strict_json(payload, name)
    required = {"round", "signature", "previous_signature"}
    if not isinstance(value, dict) or set(value) not in (required, required | {"randomness"}):
        raise ValueError(f"{name} fields differ from the frozen v2 beacon schema")
    if type(value["round"]) is not int or value["round"] != expected_round:
        raise ValueError(f"{name} round differs from the frozen round")
    signature = _require_hex(value["signature"], f"{name} signature", 96)
    previous = value["previous_signature"]
    if not isinstance(previous, str) or previous != previous.lower():
        raise ValueError(f"{name} previous_signature must be lowercase hexadecimal")
    try:
        previous_bytes = bytes.fromhex(previous)
    except ValueError as exc:
        raise ValueError(f"{name} previous_signature must be hexadecimal") from exc
    expected_previous_length = 32 if expected_round == 1 else 96
    if len(previous_bytes) != expected_previous_length:
        raise ValueError(f"{name} previous_signature has the wrong length")
    randomness = hashlib.sha256(bytes.fromhex(signature)).hexdigest()
    if "randomness" in value:
        supplied = _require_hex(value["randomness"], f"{name} randomness", 32)
        if supplied != randomness:
            raise ValueError(f"{name} randomness is not SHA-256(signature)")
    return _ParsedBeacon(expected_round, randomness, signature, previous)


def _run_pinned_drand_verifier(
    *,
    lock: Mapping[str, Any],
    official_module_path: Path,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Invoke the exact Node runtime, adapter, and official module in the lock."""

    runtime = lock["runtime"]
    node = Path(runtime["node_path"])
    if hashlib.sha256(_read_regular_file(node, 256_000_000)).hexdigest() != runtime[
        "node_sha256"
    ]:
        raise ValueError("Node runtime differs from the frozen verifier lock")
    node_version = subprocess.run(
        [str(node), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env={"HOME": os.devnull, "PATH": "/usr/bin:/bin"},
    )
    if (
        node_version.returncode != 0
        or node_version.stderr
        or node_version.stdout.decode("ascii", "strict").strip() != runtime["node_version"]
    ):
        raise ValueError("Node runtime version differs from the frozen verifier lock")

    adapter = _resolve_manifest_file(_TOOLS_DIR, lock["adapter"]["path"], "drand adapter")
    if hashlib.sha256(_read_regular_file(adapter, _MAX_JSON_BYTES)).hexdigest() != lock[
        "adapter"
    ]["sha256"]:
        raise ValueError("drand verifier adapter digest mismatch")
    official = lock["official_verifier"]
    if hashlib.sha256(
        _read_regular_file(official_module_path, 2_000_000)
    ).hexdigest() != official["module_sha256"]:
        raise ValueError("official drand-client module digest mismatch")

    with tempfile.TemporaryDirectory(prefix="drand-offline-verify-") as temporary:
        request_path = Path(temporary) / "request.json"
        request_path.write_bytes(_canonical_json(request))
        completed = subprocess.run(
            [str(node), str(adapter), str(official_module_path.resolve()), str(request_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
            cwd=temporary,
            env={
                "HOME": os.devnull,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
            },
        )
    if completed.returncode != 0 or completed.stderr:
        raise ValueError("official drand BLS verification failed")
    if len(completed.stdout) > _MAX_JSON_BYTES:
        raise ValueError("drand verifier output exceeds size bound")
    result = _strict_json(completed.stdout, "drand verifier output")
    if completed.stdout != _canonical_json(result):
        raise ValueError("drand verifier output is not canonical single-record JSON")
    return result


@dataclass(frozen=True, slots=True)
class FinalEvaluationPlan:
    """Post-freeze entropy plan rooted in one complete formal matrix.

    ``candidate_bundle_digest`` is retained as the v1 wire-field name.  In the
    formal matrix path its value is the unique pre-beacon
    :class:`CompleteFormalMatrix` root, never one candidate or one result cell.
    """

    candidate_bundle_digest: str
    freeze_attested_epoch: int
    freeze_receipt_digest: str
    freeze_bitcoin_height: int
    freeze_bitcoin_block_hash: str
    beacon_not_before_epoch: int
    beacon_round: int
    entropy_target_bitcoin_height: int | None = None
    freeze_authority_kind: str = FREEZE_AUTHORITY_DIAGNOSTIC
    chain_hash: str = DRAND_CHAIN_HASH
    _token: object = field(default=None, init=False, repr=False, compare=False)
    _formal_authority_guard: object = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        for name in (
            "candidate_bundle_digest",
            "freeze_receipt_digest",
            "freeze_bitcoin_block_hash",
            "chain_hash",
        ):
            object.__setattr__(self, name, _require_digest(getattr(self, name), name))
        if type(self.freeze_attested_epoch) is not int or (
            self.freeze_attested_epoch < DRAND_GENESIS_TIME
        ):
            raise ValueError("freeze attested epoch is invalid")
        if type(self.freeze_bitcoin_height) is not int or self.freeze_bitcoin_height < 0:
            raise ValueError("freeze Bitcoin height is invalid")
        if type(self.beacon_not_before_epoch) is not int or (
            self.beacon_not_before_epoch
            < self.freeze_attested_epoch + FINAL_BEACON_DELAY_SEC
        ):
            raise ValueError("beacon round is not delayed beyond the freeze declaration")
        if type(self.beacon_round) is not int or self.beacon_round < 1:
            raise ValueError("beacon round must be positive")
        expected_round = (
            math.ceil(
                (self.beacon_not_before_epoch - DRAND_GENESIS_TIME)
                / DRAND_PERIOD_SEC
            )
            + 1
        )
        if self.beacon_round != expected_round:
            raise ValueError("beacon round differs from the deterministic delay formula")
        target_height = self.entropy_target_bitcoin_height
        if target_height is None:
            target_height = self.freeze_bitcoin_height + FUTURE_BITCOIN_BLOCK_OFFSET
            object.__setattr__(self, "entropy_target_bitcoin_height", target_height)
        if type(target_height) is not int or target_height != (
            self.freeze_bitcoin_height + FUTURE_BITCOIN_BLOCK_OFFSET
        ):
            raise ValueError("future entropy target must be the frozen H+K block")
        if self.freeze_authority_kind not in {
            FREEZE_AUTHORITY_DIAGNOSTIC,
            FREEZE_AUTHORITY_EXTERNAL,
        }:
            raise ValueError("entropy plan has an unknown freeze authority kind")

    @classmethod
    def after_candidate_freeze(
        cls,
        candidate_bundle_digest: str,
        freeze: VerifiedCandidateFreeze,
    ) -> "FinalEvaluationPlan":
        digest = _require_digest(candidate_bundle_digest, "candidate_bundle_digest")
        if not isinstance(freeze, VerifiedCandidateFreeze):
            raise ValueError("final evaluation requires a typed candidate freeze")
        freeze._assert_for(digest)
        not_before = freeze.attested_epoch + FINAL_BEACON_DELAY_SEC
        round_number = math.ceil((not_before - DRAND_GENESIS_TIME) / DRAND_PERIOD_SEC) + 1
        if round_number < 1:
            raise ValueError("candidate freeze predates the randomness chain")
        plan = cls(
            candidate_bundle_digest=digest,
            freeze_attested_epoch=freeze.attested_epoch,
            freeze_receipt_digest=freeze.receipt_digest,
            freeze_bitcoin_height=freeze.bitcoin_height,
            freeze_bitcoin_block_hash=freeze.bitcoin_block_hash,
            beacon_not_before_epoch=not_before,
            beacon_round=round_number,
            entropy_target_bitcoin_height=(
                freeze.bitcoin_height + FUTURE_BITCOIN_BLOCK_OFFSET
            ),
            freeze_authority_kind=freeze.authority_kind,
        )
        object.__setattr__(plan, "_token", _FINAL_PLAN_TOKEN)
        return plan

    @classmethod
    def after_complete_matrix_freeze(
        cls,
        complete_formal_matrix_root_digest: str,
        freeze: VerifiedCandidateFreeze,
    ) -> "FinalEvaluationPlan":
        """Typed diagnostic entry point; v1 storage keeps the legacy field name."""

        return cls.after_candidate_freeze(complete_formal_matrix_root_digest, freeze)

    @property
    def complete_formal_matrix_root_digest(self) -> str:
        return self.candidate_bundle_digest

    def _assert_formal(self) -> None:
        self._assert_derivation_authority()
        guard = self._formal_authority_guard
        if (
            not FORMAL_FUTURE_ENTROPY_AVAILABLE
            or self.freeze_authority_kind != FREEZE_AUTHORITY_EXTERNAL
            or not callable(guard)
            or guard(self) is not True
        ):
            raise ValueError(FORMAL_FUTURE_ENTROPY_UNAVAILABLE_REASON)

    def _assert_derivation_authority(self) -> None:
        if self._token is not _FINAL_PLAN_TOKEN:
            raise ValueError("entropy plan was not created from the pinned diagnostic freeze path")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_bundle_digest": self.candidate_bundle_digest,
            "freeze_attested_epoch": self.freeze_attested_epoch,
            "freeze_receipt_digest": self.freeze_receipt_digest,
            "freeze_bitcoin_height": self.freeze_bitcoin_height,
            "freeze_bitcoin_block_hash": self.freeze_bitcoin_block_hash,
            "freeze_authority_kind": self.freeze_authority_kind,
            "entropy_target_bitcoin_height": self.entropy_target_bitcoin_height,
            "future_bitcoin_block_offset": FUTURE_BITCOIN_BLOCK_OFFSET,
            "beacon_not_before_epoch": self.beacon_not_before_epoch,
            "beacon_round": self.beacon_round,
            "chain_hash": self.chain_hash,
            "public_key": DRAND_PUBLIC_KEY,
            "period_sec": DRAND_PERIOD_SEC,
            "genesis_time": DRAND_GENESIS_TIME,
            "root_semantics": "candidate_bundle_digest_is_complete_formal_matrix_root",
            "formal_strength_available": FORMAL_FUTURE_ENTROPY_AVAILABLE,
            "formal_unavailable_reason": FORMAL_FUTURE_ENTROPY_UNAVAILABLE_REASON,
            "diagnostic_derivation": "HMAC-SHA256(drand_randomness, contract|complete_formal_matrix_root|namespace|index)",
            "formal_derivation": "HMAC-SHA256(SHA256(future_bitcoin_block_hash||drand_randomness||authority_receipts), contract|complete_formal_matrix_root|namespace|index)",
            "formal_deck_output": "full_32_byte_HMAC_digest_as_unsigned_big_endian_uint256",
            "formal_deck_root_pool_size": FORMAL_DECK_ROOT_POOL_SIZE,
            "formal_deck_namespace_template": FORMAL_DECK_NAMESPACE_TEMPLATE,
            "formal_policy_output": "first_8_HMAC_bytes_as_unsigned_big_endian_then_mask_to_63_bits",
            "formal_policy_namespace_template": FORMAL_POLICY_NAMESPACE_TEMPLATE,
        }

    def verify_beacon(
        self,
        evidence_manifest_path: str | os.PathLike[str],
        official_module_path: str | os.PathLike[str],
    ) -> "VerifiedBeacon":
        """Re-read relay files and verify BLS, without granting freeze timing authority."""

        self._assert_derivation_authority()
        lock = _load_pinned_lock(
            _DRAND_LOCK_PATH,
            DRAND_VERIFIER_LOCK_SHA256,
            "drand-verifier-lock-v1",
        )
        manifest_path = Path(evidence_manifest_path)
        manifest_payload = _read_regular_file(manifest_path, _MAX_JSON_BYTES)
        manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
        manifest = _require_exact_keys(
            _strict_json(manifest_payload, "drand evidence manifest"),
            {"schema", "chain_hash", "round", "observations"},
            "drand evidence manifest",
        )
        if manifest["schema"] != "drand-cross-fetch-evidence-v1":
            raise ValueError("unexpected drand evidence manifest schema")
        if _require_hex(manifest["chain_hash"], "manifest chain hash", 32) != self.chain_hash:
            raise ValueError("drand evidence belongs to a different chain")
        if type(manifest["round"]) is not int or manifest["round"] != self.beacon_round:
            raise ValueError("drand evidence belongs to a different round")
        observations = manifest["observations"]
        if not isinstance(observations, list) or len(observations) != len(DRAND_ENDPOINTS):
            raise ValueError("all frozen drand relays must be cross-fetched")

        root = manifest_path.resolve().parent
        expected_row_keys = {
            "endpoint",
            "chain_info_file",
            "chain_info_sha256",
            "beacon_file",
            "beacon_sha256",
        }
        if self.beacon_round > 1:
            expected_row_keys |= {"previous_beacon_file", "previous_beacon_sha256"}
        seen_endpoints: set[str] = set()
        infos: list[dict[str, Any]] = []
        current_beacons: list[_ParsedBeacon] = []
        previous_beacons: list[_ParsedBeacon] = []
        raw_receipts: list[dict[str, str]] = []
        for index, untyped_row in enumerate(observations, start=1):
            row = _require_exact_keys(
                untyped_row, expected_row_keys, f"drand observation {index}"
            )
            endpoint = row["endpoint"]
            if endpoint not in DRAND_ENDPOINTS or endpoint in seen_endpoints:
                raise ValueError("drand relay set differs from the frozen contract")
            seen_endpoints.add(endpoint)

            info_path = _resolve_manifest_file(
                root, row["chain_info_file"], f"relay {index} chain info file"
            )
            beacon_path = _resolve_manifest_file(
                root, row["beacon_file"], f"relay {index} beacon file"
            )
            info_payload = _read_regular_file(info_path, _MAX_JSON_BYTES)
            beacon_payload = _read_regular_file(beacon_path, _MAX_JSON_BYTES)
            info_digest = hashlib.sha256(info_payload).hexdigest()
            beacon_digest = hashlib.sha256(beacon_payload).hexdigest()
            if info_digest != _require_digest(
                row["chain_info_sha256"], f"relay {index} chain info digest"
            ):
                raise ValueError("saved drand chain info digest mismatch")
            if beacon_digest != _require_digest(
                row["beacon_sha256"], f"relay {index} beacon digest"
            ):
                raise ValueError("saved drand beacon digest mismatch")
            infos.append(_parse_chain_info(info_payload, f"relay {index} chain info"))
            current = _parse_beacon(
                beacon_payload, self.beacon_round, f"relay {index} beacon"
            )
            current_beacons.append(current)
            raw_receipt = {
                "endpoint": endpoint,
                "chain_info_sha256": info_digest,
                "beacon_sha256": beacon_digest,
            }
            if self.beacon_round == 1:
                if current.previous_signature != DRAND_GENESIS_SEED:
                    raise ValueError("round 1 previous_signature is not the genesis seed")
            else:
                previous_path = _resolve_manifest_file(
                    root,
                    row["previous_beacon_file"],
                    f"relay {index} previous beacon file",
                )
                previous_payload = _read_regular_file(previous_path, _MAX_JSON_BYTES)
                previous_digest = hashlib.sha256(previous_payload).hexdigest()
                if previous_digest != _require_digest(
                    row["previous_beacon_sha256"],
                    f"relay {index} previous beacon digest",
                ):
                    raise ValueError("saved previous drand beacon digest mismatch")
                previous = _parse_beacon(
                    previous_payload,
                    self.beacon_round - 1,
                    f"relay {index} previous beacon",
                )
                if current.previous_signature != previous.signature:
                    raise ValueError("drand previous_signature does not link to round N-1")
                previous_beacons.append(previous)
                raw_receipt["previous_beacon_sha256"] = previous_digest
            raw_receipts.append(raw_receipt)

        if seen_endpoints != set(DRAND_ENDPOINTS):
            raise ValueError("drand relay set differs from the frozen contract")
        first_info = infos[0]
        if any(info != first_info for info in infos[1:]):
            raise ValueError("drand relays disagree on chain info")
        first_current = current_beacons[0]
        if any(beacon != first_current for beacon in current_beacons[1:]):
            raise ValueError("drand relays disagree on the selected beacon")
        if previous_beacons and any(
            beacon != previous_beacons[0] for beacon in previous_beacons[1:]
        ):
            raise ValueError("drand relays disagree on the previous beacon")

        beacons = []
        if previous_beacons:
            beacons.append(previous_beacons[0].to_official_dict())
        beacons.append(first_current.to_official_dict())
        request = {
            "schema": "drand-offline-verification-v1",
            "chain_info": {
                "public_key": DRAND_PUBLIC_KEY,
                "period": DRAND_PERIOD_SEC,
                "genesis_time": DRAND_GENESIS_TIME,
                "hash": DRAND_CHAIN_HASH,
                "groupHash": DRAND_GENESIS_SEED,
                "schemeID": DRAND_SCHEME,
                "metadata": {"beaconID": DRAND_BEACON_ID},
            },
            "beacons": beacons,
        }
        result = _run_pinned_drand_verifier(
            lock=lock,
            official_module_path=Path(official_module_path),
            request=request,
        )
        expected_result = {
            "schema": "drand-offline-verification-result-v1",
            "verified": True,
            "verified_rounds": [beacon["round"] for beacon in beacons],
        }
        if result != expected_result:
            raise ValueError("official drand verifier did not return exact success evidence")
        receipt = {
            "chain_hash": self.chain_hash,
            "round": self.beacon_round,
            "randomness": first_current.randomness,
            "signature": first_current.signature,
            "previous_signature": first_current.previous_signature,
            "evidence_manifest_sha256": manifest_digest,
            "raw_payloads": raw_receipts,
            "drand_verifier_lock_sha256": DRAND_VERIFIER_LOCK_SHA256,
            "official_module_sha256": lock["official_verifier"]["module_sha256"],
            "node_sha256": lock["runtime"]["node_sha256"],
        }
        receipt_digest = hashlib.sha256(_canonical_json(receipt)).hexdigest()
        verified = VerifiedBeacon(
            chain_hash=self.chain_hash,
            round=self.beacon_round,
            randomness=first_current.randomness,
            signature=first_current.signature,
            previous_signature=first_current.previous_signature,
            receipt_digest=receipt_digest,
        )
        object.__setattr__(verified, "_token", _VERIFIED_BEACON_TOKEN)
        return verified

    def derive_seeds(
        self, beacon: "VerifiedBeacon", namespace: str, count: int
    ) -> tuple[int, ...]:
        """Low-level compatibility API restricted to registered formal namespaces."""

        self._assert_derivation_authority()
        beacon._assert_for(self)
        randomness = beacon._seed_material_for(self)
        namespace = _validate_formal_seed_namespace(namespace)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("final seed count must be a positive integer")
        is_deck_root_pool = namespace.endswith("/deck-root")
        if is_deck_root_pool and count != FORMAL_DECK_ROOT_POOL_SIZE:
            raise ValueError(
                f"formal deck roots must be derived once as the exact "
                f"{FORMAL_DECK_ROOT_POOL_SIZE}-root pool"
            )
        if not is_deck_root_pool and count > FORMAL_DECK_ROOT_POOL_SIZE:
            raise ValueError("formal policy matrix exceeds the frozen deck-root pool")
        seeds: list[int] = []
        for index in range(count):
            message = (
                f"{CONTRACT_VERSION}|{self.candidate_bundle_digest}|{namespace}|{index}"
            ).encode("utf-8")
            digest = hmac.new(randomness, message, hashlib.sha256).digest()
            if is_deck_root_pool:
                seeds.append(int.from_bytes(digest, "big"))
            else:
                seeds.append(int.from_bytes(digest[:8], "big") & ((1 << 63) - 1))
        if len(set(seeds)) != len(seeds):
            raise ValueError("derived final seed collision")
        return tuple(seeds)

    def derive_formal_deck_root_pool(
        self,
        beacon: "VerifiedBeacon",
        seed_cohort_digest: str,
    ) -> tuple[int, ...]:
        """Derive one shared 8,192-entry uint256 deck-root cohort pool."""

        return self.derive_seeds(
            beacon,
            formal_deck_seed_namespace(seed_cohort_digest),
            FORMAL_DECK_ROOT_POOL_SIZE,
        )

    def derive_formal_policy_seeds(
        self,
        beacon: "VerifiedBeacon",
        seed_cohort_digest: str,
        artifact_identity_digest: str,
        matrix_block_count: int,
    ) -> tuple[int, ...]:
        """Derive one identity's uint63 policy stream for the frozen matrix size."""

        return self.derive_seeds(
            beacon,
            formal_policy_seed_namespace(
                seed_cohort_digest,
                artifact_identity_digest,
            ),
            matrix_block_count,
        )

    def derive_formal_analysis_seed(
        self,
        beacon: "VerifiedBeacon",
        *,
        seed_cohort_digest: str,
        hypothesis_digest: str,
        analysis_domain: str,
    ) -> int:
        """Derive bootstrap/sign-flip RNG from frozen future entropy.

        There is intentionally no caller-provided integer seed.  The stream is
        bound to the matrix root, the candidate-neutral deck cohort, the exact
        preregistered hypothesis, and a typed analysis domain.
        """

        self._assert_derivation_authority()
        beacon._assert_for(self)
        namespace = formal_analysis_seed_namespace(
            seed_cohort_digest,
            analysis_domain,
            hypothesis_digest,
        )
        message = (
            f"{CONTRACT_VERSION}|{self.candidate_bundle_digest}|{namespace}|0"
        ).encode("utf-8")
        digest = hmac.new(beacon._seed_material_for(self), message, hashlib.sha256).digest()
        return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)

    def rank_opponents(
        self,
        beacon: "VerifiedBeacon",
        opponent_artifact_hashes: Sequence[str],
    ) -> tuple[str, ...]:
        self._assert_derivation_authority()
        beacon._assert_for(self)
        randomness = beacon._seed_material_for(self)
        normalized = tuple(
            _require_digest(value, "opponent artifact hash")
            for value in opponent_artifact_hashes
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("opponent universe contains duplicate artifact hashes")
        return tuple(
            sorted(
                normalized,
                key=lambda value: hmac.new(
                    randomness,
                    (
                        f"{CONTRACT_VERSION}|{self.candidate_bundle_digest}|opponent|{value}"
                    ).encode("utf-8"),
                    hashlib.sha256,
                ).digest(),
            )
        )


@dataclass(frozen=True, slots=True)
class VerifiedBeacon:
    chain_hash: str
    round: int
    randomness: str
    signature: str
    previous_signature: str
    receipt_digest: str
    future_bitcoin_block_height: int | None = None
    future_bitcoin_block_hash: str | None = None
    independent_chainwork_receipt_digest: str | None = None
    independent_witness_receipt_digest: str | None = None
    formal_entropy_mix_digest: str | None = None
    _token: object = field(default=None, init=False, repr=False, compare=False)
    _formal_authority_guard: object = field(
        default=None, init=False, repr=False, compare=False
    )

    def _assert_for(self, plan: FinalEvaluationPlan) -> None:
        if self._token is not _VERIFIED_BEACON_TOKEN:
            raise ValueError("beacon was not created by formal BLS verification")
        if self.chain_hash != plan.chain_hash or self.round != plan.beacon_round:
            raise ValueError("verified beacon does not belong to this final plan")
        _require_digest(self.randomness, "beacon randomness")
        _require_hex(self.signature, "beacon signature", 96)
        expected_previous_length = 32 if self.round == 1 else 96
        _require_hex(
            self.previous_signature,
            "beacon previous_signature",
            expected_previous_length,
        )
        _require_digest(self.receipt_digest, "beacon receipt digest")

    def _assert_formal_for(self, plan: FinalEvaluationPlan) -> None:
        """Require an independently witnessed H+K block combined with drand.

        A valid BLS signature proves the drand value, but it does not prove the
        value was unknown when a caller-controlled freeze actually happened.
        The formal seed therefore also commits to a fixed future mainnet block
        and independent chainwork/witness receipts.  No installed authority can
        currently issue this capability, so this path intentionally fails
        closed.
        """

        plan._assert_formal()
        self._assert_for(plan)
        guard = self._formal_authority_guard
        if (
            not callable(guard)
            or guard(self) is not True
            or self.future_bitcoin_block_height
            != plan.entropy_target_bitcoin_height
            or self.future_bitcoin_block_hash is None
            or self.independent_chainwork_receipt_digest is None
            or self.independent_witness_receipt_digest is None
            or self.formal_entropy_mix_digest is None
        ):
            raise ValueError(FORMAL_FUTURE_ENTROPY_UNAVAILABLE_REASON)
        block_hash = _require_digest(
            self.future_bitcoin_block_hash, "future Bitcoin block hash"
        )
        chainwork = _require_digest(
            self.independent_chainwork_receipt_digest,
            "independent chainwork receipt",
        )
        witness = _require_digest(
            self.independent_witness_receipt_digest,
            "independent future-entropy witness receipt",
        )
        expected_mix = hashlib.sha256(
            _canonical_json(
                {
                    "chainwork_receipt_digest": chainwork,
                    "drand_randomness": self.randomness,
                    "drand_receipt_digest": self.receipt_digest,
                    "future_bitcoin_block_hash": block_hash,
                    "future_bitcoin_block_height": self.future_bitcoin_block_height,
                    "schema": "formal-future-bitcoin-drand-entropy-mix-v1",
                    "witness_receipt_digest": witness,
                }
            )
        ).hexdigest()
        if self.formal_entropy_mix_digest != expected_mix:
            raise ValueError("formal future entropy mix differs from bound evidence")

    def _seed_material_for(self, plan: FinalEvaluationPlan) -> bytes:
        if plan.freeze_authority_kind == FREEZE_AUTHORITY_EXTERNAL:
            self._assert_formal_for(plan)
            assert self.formal_entropy_mix_digest is not None
            return bytes.fromhex(self.formal_entropy_mix_digest)
        self._assert_for(plan)
        return bytes.fromhex(self.randomness)
