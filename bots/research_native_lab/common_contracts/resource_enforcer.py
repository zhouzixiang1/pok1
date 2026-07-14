"""Fail-closed resource authority contract for native evaluations.

The historical same-UID writable-cgroup launcher is permanently diagnostic.
Formal authority requires the fixed, root-owned external supervisor contract,
two distinct low-privilege candidate identities, a supervisor-owned cgroup-v2
tree, read-only CAS materialization, per-decision enforcement, signed typed raw
evidence, and a durable cleanup receipt.  If that installation is absent the
formal path is unavailable; there is no RLIMIT or in-process fallback.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import ipaddress
import json
import os
import secrets
import signal
import socket
import stat
import struct
import subprocess
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


RESOURCE_ENFORCER_ALGORITHM_ID = "pok-formal-cgroup-v2-enforcer-v1"
RESOURCE_ENFORCER_ALGORITHM_SPEC = (
    "trusted-root-supervisor;externally-verified-signed-attestation;"
    "fixed-global-flock;two-distinct-unprivileged-uids;private-proc;"
    "supervisor-owned-linux-cgroup-v2;two-disjoint-cpuset-domain-children;"
    "cpu.max;memory.max;memory.swap.max=0;pids.max;memory.oom.group=1;"
    "readonly-cas-artifact-materialization;minimal-exact-env;"
    "two-stage-signed-launch+postrun-capture;challenge+control-session-binding;"
    "socket-fd+inode+cookie+netns+endpoint-binding;"
    "raw-wire+wire-semantics+replay-digest-binding;"
    "decision-to-wire-open+nullable-action-ingress+peer-relay-close-binding-v3;"
    "root-owned-receipt-ledger;"
    "signed-contiguous-attempt-journal+closed-head;"
    "downstream-durable-one-shot-receipt-consumption;"
    "setsid;attach-before-exec;per-decision-hard-stop;"
    "cgroup.kill+killpg-infrastructure-wall-stop;"
    "memory.peak;pids.peak;memory.swap.current;cpu.stat;memory.events;"
    "pids.events;typed-cross-linked-execution-resource-records;"
    "durable-cleanup-receipt;inode+mount+controller+config-snapshot;canonical-json-v1;"
    "no-rlimit-fallback"
)
RESOURCE_ENFORCER_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
RESOURCE_ENFORCER_DIGEST = hashlib.sha256(
    RESOURCE_ENFORCER_ALGORITHM_SPEC.encode("ascii")
    + b"\x00"
    + bytes.fromhex(RESOURCE_ENFORCER_SOURCE_SHA256)
).hexdigest()
REQUIRED_CONTROLLERS = ("cpu", "cpuset", "memory", "pids")
REQUIRED_CONTROLLER_FILES = (
    "cgroup.controllers",
    "cgroup.procs",
    "cgroup.subtree_control",
    "cgroup.type",
    "cpuset.cpus.effective",
    "cpuset.mems.effective",
)
REQUIRED_CHILD_FILES = (
    "cgroup.events",
    "cgroup.kill",
    "cgroup.procs",
    "cpu.max",
    "cpu.stat",
    "cpuset.cpus",
    "cpuset.mems",
    "memory.current",
    "memory.events",
    "memory.max",
    "memory.oom.group",
    "memory.peak",
    "memory.swap.current",
    "memory.swap.max",
    "pids.current",
    "pids.events",
    "pids.max",
    "pids.peak",
)
THREAD_ENVIRONMENT_KEYS = (
    "MKL_DYNAMIC",
    "MKL_NUM_THREADS",
    "NUMBA_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "OMP_DYNAMIC",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "RAYON_NUM_THREADS",
    "TOKENIZERS_PARALLELISM",
    "VECLIB_MAXIMUM_THREADS",
)
MAX_TEXT_BYTES = 1_048_576
FORMAL_GLOBAL_LOCK_PATH = Path("/run/lock/pok-formal-evaluation.lock")
TRUSTED_SUPERVISOR_PROTOCOL = "pok-trusted-resource-supervisor-v1"
SUPERVISOR_BACKEND_KIND = "trusted-supervisor-cgroup-v2"
TRUSTED_SUPERVISOR_CONTRACT_PATH = Path(
    "/etc/pok/formal-resource-supervisor-v1.json"
)
TRUSTED_SUPERVISOR_ATTESTATION_PATH = Path(
    "/run/pok-formal/supervisor-attestation-v1.json"
)
TRUSTED_SUPERVISOR_LEDGER_ROOT = Path(
    "/var/lib/pok-formal/receipt-ledger-v1"
)
TRUSTED_SUPERVISOR_ATTEMPT_JOURNAL_ROOT = Path(
    "/var/lib/pok-formal/attempt-journal-v1"
)
ARTIFACT_ROLES = (
    "action_set",
    "config",
    "dependency",
    "model",
    "runtime",
)
DECISION_ENFORCEMENT_EVENT_SCHEMA = (
    "pok-supervisor-decision-enforcement-event-v3"
)
DECISION_IDENTITY_SCHEMA = "pok-supervisor-decision-identity-v3"


class ResourceEnforcementError(RuntimeError):
    pass


class FormalEnforcementUnavailable(ResourceEnforcementError):
    pass


class GlobalLeaseTimeout(ResourceEnforcementError):
    pass


class ResourceCleanupError(ResourceEnforcementError):
    def __init__(self, message: str, evidence: object) -> None:
        super().__init__(message)
        self.evidence = evidence


_FORMAL_EVIDENCE_REGISTRY: dict[int, weakref.ReferenceType["ConnectionEvidence"]] = {}
# These sets prevent accidental duplicate conversion inside one evaluator
# process only.  They are not, and are never described as, restart-durable
# authority.  Cross-restart uniqueness is supplied by the signed root-owned
# receipt ledger, the closed attempt journal, and the downstream formal-result
# ledger's persistent rejection of a repeated ``receipt_consumption_key``.
_CONSUMED_SUPERVISOR_RECEIPTS: set[str] = set()
_CONVERTED_SUPERVISOR_BRIDGES: set[str] = set()
_EMITTED_FORMAL_CONNECTION_RECEIPTS: set[tuple[str, int]] = set()


def _has_formal_capability(evidence: "ConnectionEvidence") -> bool:
    reference = _FORMAL_EVIDENCE_REGISTRY.get(id(evidence))
    return reference is not None and reference() is evidence


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def required_candidate_sandbox_policy_digest() -> str:
    return canonical_digest(
        {
            "authenticated_preopened_game_fd_only": True,
            "candidate_control_socket_visibility": "none",
            "candidate_to_candidate_ipc": "none",
            "landlock_cas_read_only": True,
            "mount_namespace": "distinct_per_connection",
            "network_namespace": "distinct_per_connection_no_new_sockets",
            "new_socket_connect_bind_listen": "seccomp_denied",
            "no_new_privs": True,
            "pid_and_proc_namespace": "distinct_private_proc_hidepid",
            "private_dev_shm_tmpfs": True,
            "private_tmp_tmpfs": True,
            "schema": "pok-formal-candidate-sandbox-policy-v1",
            "sysv_and_posix_ipc_namespace": "distinct_per_connection",
        }
    )


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a SHA-256 digest")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be hexadecimal") from exc
    if len(decoded) != 32:
        raise ValueError(f"{name} must be a 32-byte digest")
    return value.lower()


def _strict_int(value: int, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _strict_absolute_path(value: str, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be an absolute NUL-free path")
    path = Path(value)
    if not path.is_absolute() or path != Path(os.path.abspath(value)):
        raise ValueError(f"{name} must be normalized and absolute")
    return path


def _read_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _root_owned_not_mutable(path: Path, *, expected_kind: str) -> tuple[bool, str]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        return False, f"{path} is unavailable: {type(exc).__name__}"
    if stat.S_ISLNK(metadata.st_mode):
        return False, f"{path} is a symlink"
    predicates = {
        "directory": stat.S_ISDIR,
        "regular": stat.S_ISREG,
        "socket": stat.S_ISSOCK,
    }
    if expected_kind not in predicates or not predicates[expected_kind](metadata.st_mode):
        return False, f"{path} is not a {expected_kind}"
    if metadata.st_uid != 0:
        return False, f"{path} is not owned by uid 0"
    if metadata.st_mode & 0o022:
        return False, f"{path} is group/world mutable"
    return True, ""


def _root_owned_chain_not_mutable(
    path: Path, *, expected_kind: str
) -> tuple[bool, str]:
    """Verify an object and every traversed directory without following links.

    Checking only the leaf is insufficient: an unprivileged writer of any
    parent directory could atomically replace an otherwise root-owned leaf.
    Formal installation paths therefore have to be absolute, normalized, and
    rooted in a completely root-owned, non-group/world-writable path chain.
    """

    try:
        normalized = _strict_absolute_path(str(path), "trusted installation path")
    except ValueError as exc:
        return False, str(exc)
    current = Path("/")
    for index, component in enumerate(normalized.parts[1:]):
        current = current / component
        kind = expected_kind if index == len(normalized.parts[1:]) - 1 else "directory"
        ok, reason = _root_owned_not_mutable(current, expected_kind=kind)
        if not ok:
            return False, reason
    return True, ""


@dataclass(frozen=True, slots=True)
class TrustedSupervisorContract:
    """Root-owned installation contract for the only formal launch authority.

    The fixed contract file is not caller-selectable.  The supervisor owns all
    cgroup control files and the artifact CAS.  It launches the two candidates
    under distinct non-privileged UIDs and authenticates the evaluator from a
    pre-opened control channel by peer cgroup, so a candidate cannot ask the
    service to move/kill a peer even when it discovers the socket pathname.
    """

    schema: str
    supervisor_executable: str
    supervisor_executable_sha256: str
    verifier_executable: str
    verifier_executable_sha256: str
    public_key_path: str
    public_key_sha256: str
    control_socket_path: str
    control_cgroup_root: str
    artifact_cas_root: str
    consumption_ledger_root: str
    attempt_journal_root: str
    global_lock_path: str
    service_uid: int
    evaluator_uid: int
    bot_uids_by_connection: tuple[int, int]
    peer_cgroup_auth_required: bool
    preopened_control_fd_required: bool
    private_proc_required: bool
    readonly_artifact_mount_required: bool
    candidate_sandbox_policy_digest: str
    separate_network_namespace_required: bool
    separate_ipc_namespace_required: bool
    separate_mount_namespace_required: bool
    private_tmpfs_required: bool
    no_new_privs_required: bool
    seccomp_socket_lockdown_required: bool
    landlock_required: bool
    authenticated_game_fd_only_required: bool
    durable_consumption_ledger_required: bool
    consumption_no_clobber_required: bool
    consumption_fsync_required: bool
    durable_attempt_journal_required: bool
    attempt_journal_no_clobber_required: bool
    attempt_journal_fsync_required: bool

    @classmethod
    def from_fixed_file(
        cls, path: Path = TRUSTED_SUPERVISOR_CONTRACT_PATH
    ) -> "TrustedSupervisorContract":
        if path != TRUSTED_SUPERVISOR_CONTRACT_PATH:
            raise FormalEnforcementUnavailable(
                "formal supervisor contract path is fixed by the enforcer"
            )
        ok, reason = _root_owned_chain_not_mutable(path, expected_kind="regular")
        if not ok:
            raise FormalEnforcementUnavailable(reason)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FormalEnforcementUnavailable(
                "formal supervisor contract is unreadable"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {
            "artifact_cas_root",
            "attempt_journal_fsync_required",
            "attempt_journal_no_clobber_required",
            "attempt_journal_root",
            "authenticated_game_fd_only_required",
            "bot_uids_by_connection",
            "candidate_sandbox_policy_digest",
            "consumption_fsync_required",
            "consumption_ledger_root",
            "consumption_no_clobber_required",
            "control_cgroup_root",
            "control_socket_path",
            "evaluator_uid",
            "global_lock_path",
            "landlock_required",
            "durable_consumption_ledger_required",
            "durable_attempt_journal_required",
            "no_new_privs_required",
            "peer_cgroup_auth_required",
            "preopened_control_fd_required",
            "private_proc_required",
            "private_tmpfs_required",
            "public_key_path",
            "public_key_sha256",
            "readonly_artifact_mount_required",
            "schema",
            "seccomp_socket_lockdown_required",
            "separate_ipc_namespace_required",
            "separate_mount_namespace_required",
            "separate_network_namespace_required",
            "service_uid",
            "supervisor_executable",
            "supervisor_executable_sha256",
            "verifier_executable",
            "verifier_executable_sha256",
        }:
            raise FormalEnforcementUnavailable(
                "formal supervisor contract has unknown or missing fields"
            )
        try:
            contract = cls(
                **{
                    **payload,
                    "bot_uids_by_connection": tuple(payload["bot_uids_by_connection"]),
                }
            )
            contract.validate()
        except (TypeError, ValueError) as exc:
            raise FormalEnforcementUnavailable(
                "formal supervisor contract is invalid"
            ) from exc
        return contract

    def validate(self) -> None:
        if self.schema != TRUSTED_SUPERVISOR_PROTOCOL:
            raise ValueError("unknown trusted supervisor protocol")
        for name in (
            "supervisor_executable",
            "verifier_executable",
            "public_key_path",
            "control_socket_path",
            "control_cgroup_root",
            "artifact_cas_root",
            "consumption_ledger_root",
            "attempt_journal_root",
            "global_lock_path",
        ):
            _strict_absolute_path(getattr(self, name), name)
        if len(
            {
                self.supervisor_executable,
                self.verifier_executable,
                self.public_key_path,
                self.control_socket_path,
                self.control_cgroup_root,
                self.artifact_cas_root,
                self.consumption_ledger_root,
                self.attempt_journal_root,
                self.global_lock_path,
            }
        ) != 9:
            raise ValueError("formal supervisor installation paths must be distinct")
        for name in (
            "supervisor_executable_sha256",
            "verifier_executable_sha256",
            "public_key_sha256",
            "candidate_sandbox_policy_digest",
        ):
            _digest(getattr(self, name), name)
        if self.global_lock_path != str(FORMAL_GLOBAL_LOCK_PATH):
            raise ValueError("formal global lease path is not caller-selectable")
        if self.consumption_ledger_root != str(TRUSTED_SUPERVISOR_LEDGER_ROOT):
            raise ValueError("formal consumption ledger path is not caller-selectable")
        if self.attempt_journal_root != str(TRUSTED_SUPERVISOR_ATTEMPT_JOURNAL_ROOT):
            raise ValueError("formal attempt journal path is not caller-selectable")
        if (
            self.candidate_sandbox_policy_digest
            != required_candidate_sandbox_policy_digest()
        ):
            raise ValueError("candidate sandbox policy digest differs from the enforcer")
        _strict_int(self.service_uid, "service uid")
        if self.service_uid != 0:
            raise ValueError("the formal supervisor service must run as uid 0")
        _strict_int(self.evaluator_uid, "evaluator uid", minimum=1)
        uids = tuple(self.bot_uids_by_connection)
        if (
            len(uids) != 2
            or any(type(uid) is not int or uid <= 0 for uid in uids)
            or len({self.service_uid, self.evaluator_uid, *uids}) != 4
        ):
            raise ValueError(
                "supervisor, evaluator, and both candidate UIDs must be distinct"
            )
        for name in (
            "peer_cgroup_auth_required",
            "preopened_control_fd_required",
            "private_proc_required",
            "readonly_artifact_mount_required",
            "separate_network_namespace_required",
            "separate_ipc_namespace_required",
            "separate_mount_namespace_required",
            "private_tmpfs_required",
            "no_new_privs_required",
            "seccomp_socket_lockdown_required",
            "landlock_required",
            "authenticated_game_fd_only_required",
            "durable_consumption_ledger_required",
            "consumption_no_clobber_required",
            "consumption_fsync_required",
            "durable_attempt_journal_required",
            "attempt_journal_no_clobber_required",
            "attempt_journal_fsync_required",
        ):
            if getattr(self, name) is not True:
                raise ValueError(f"{name} must be true")

    def digest(self) -> str:
        self.validate()
        return canonical_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class SupervisorAttestation:
    schema: str
    contract_digest: str
    control_session_digest: str
    boot_id: str
    supervisor_pid: int
    supervisor_start_ticks: int
    supervisor_cgroup: str
    control_cgroup_inode: int
    control_cgroup_mount_id: int
    artifact_cas_inode: int
    consumption_ledger_root_inode: int
    attempt_journal_root_inode: int
    global_lock_inode: int
    evaluator_uid: int
    bot_uids_by_connection: tuple[int, int]
    peer_cgroup_auth_verified: bool
    private_proc_verified: bool
    readonly_artifact_mount_verified: bool
    candidates_cannot_write_cgroupfs: bool
    candidates_cannot_signal_peer: bool
    candidate_sandbox_policy_digest: str
    separate_network_namespaces_verified: bool
    separate_ipc_namespaces_verified: bool
    separate_mount_namespaces_verified: bool
    private_tmpfs_verified: bool
    no_new_privs_verified: bool
    seccomp_socket_lockdown_verified: bool
    landlock_verified: bool
    authenticated_game_fd_only_verified: bool
    durable_consumption_ledger_verified: bool
    consumption_no_clobber_verified: bool
    consumption_fsync_verified: bool
    durable_attempt_journal_verified: bool
    attempt_journal_no_clobber_verified: bool
    attempt_journal_fsync_verified: bool
    issued_epoch_ms: int
    expires_epoch_ms: int
    nonce: str
    signature_hex: str

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature_hex")
        return payload

    def payload_digest(self) -> str:
        return canonical_digest(self.unsigned_payload())

    def validate(
        self,
        contract: TrustedSupervisorContract,
        *,
        now_epoch_ms: int,
        require_current_boot: bool = True,
    ) -> None:
        if self.schema != "pok-trusted-resource-supervisor-attestation-v1":
            raise ValueError("unknown supervisor attestation schema")
        if self.contract_digest != contract.digest():
            raise ValueError("supervisor attestation belongs to a different contract")
        object.__setattr__(
            self,
            "control_session_digest",
            _digest(self.control_session_digest, "supervisor control session"),
        )
        try:
            normalized_boot_id = str(uuid.UUID(self.boot_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("supervisor attestation boot ID is invalid") from exc
        if normalized_boot_id != self.boot_id:
            raise ValueError("supervisor attestation boot ID is not canonical")
        if require_current_boot is not True and require_current_boot is not False:
            raise ValueError("current-boot requirement must be boolean")
        if require_current_boot and self.boot_id != _read_boot_id():
            raise ValueError("supervisor attestation is not from this boot")
        if (
            not isinstance(self.supervisor_cgroup, str)
            or not self.supervisor_cgroup.startswith("/")
            or ".." in Path(self.supervisor_cgroup).parts
        ):
            raise ValueError("supervisor attestation has an invalid cgroup identity")
        _strict_int(self.supervisor_pid, "supervisor pid", minimum=1)
        _strict_int(self.supervisor_start_ticks, "supervisor start ticks", minimum=1)
        _strict_int(self.control_cgroup_inode, "control cgroup inode", minimum=1)
        _strict_int(self.control_cgroup_mount_id, "control cgroup mount id", minimum=1)
        _strict_int(self.artifact_cas_inode, "artifact CAS inode", minimum=1)
        _strict_int(
            self.consumption_ledger_root_inode,
            "consumption ledger root inode",
            minimum=1,
        )
        _strict_int(
            self.attempt_journal_root_inode,
            "attempt journal root inode",
            minimum=1,
        )
        _strict_int(self.global_lock_inode, "global lock inode", minimum=1)
        if self.evaluator_uid != contract.evaluator_uid:
            raise ValueError("attested evaluator uid differs from the contract")
        if tuple(self.bot_uids_by_connection) != contract.bot_uids_by_connection:
            raise ValueError("attested candidate UIDs differ from the contract")
        object.__setattr__(
            self,
            "candidate_sandbox_policy_digest",
            _digest(self.candidate_sandbox_policy_digest, "candidate sandbox policy"),
        )
        if self.candidate_sandbox_policy_digest != contract.candidate_sandbox_policy_digest:
            raise ValueError("attested candidate sandbox policy differs from the contract")
        for name in (
            "peer_cgroup_auth_verified",
            "private_proc_verified",
            "readonly_artifact_mount_verified",
            "candidates_cannot_write_cgroupfs",
            "candidates_cannot_signal_peer",
            "separate_network_namespaces_verified",
            "separate_ipc_namespaces_verified",
            "separate_mount_namespaces_verified",
            "private_tmpfs_verified",
            "no_new_privs_verified",
            "seccomp_socket_lockdown_verified",
            "landlock_verified",
            "authenticated_game_fd_only_verified",
            "durable_consumption_ledger_verified",
            "consumption_no_clobber_verified",
            "consumption_fsync_verified",
            "durable_attempt_journal_verified",
            "attempt_journal_no_clobber_verified",
            "attempt_journal_fsync_verified",
        ):
            if getattr(self, name) is not True:
                raise ValueError(f"attestation does not prove {name}")
        _strict_int(self.issued_epoch_ms, "attestation issue time", minimum=1)
        _strict_int(self.expires_epoch_ms, "attestation expiry", minimum=1)
        if not self.issued_epoch_ms <= now_epoch_ms <= self.expires_epoch_ms:
            raise ValueError("supervisor attestation is stale or not yet valid")
        if self.expires_epoch_ms - self.issued_epoch_ms > 60_000:
            raise ValueError("supervisor attestation validity exceeds 60 seconds")
        if (
            not isinstance(self.nonce, str)
            or len(self.nonce) < 32
            or len(self.nonce) > 256
            or not isinstance(self.signature_hex, str)
        ):
            raise ValueError("supervisor attestation nonce/signature is invalid")
        try:
            signature = bytes.fromhex(self.signature_hex)
        except ValueError as exc:
            raise ValueError("supervisor signature is not hexadecimal") from exc
        if len(signature) < 64 or len(signature) > 16_384:
            raise ValueError("supervisor signature has an invalid length")


@dataclass(frozen=True, slots=True)
class TrustedSupervisorProbe:
    schema: str
    contract_path: str
    attestation_path: str
    contract_digest: str | None
    attestation_digest: str | None
    control_session_digest: str | None
    formal_available: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SocketCaptureIdentity:
    """Kernel/socket identity signed by the post-run capture authority."""

    schema: str
    capture_session_digest: str
    connection_index: int
    socket_fd: int
    socket_inode: int
    socket_cookie: int
    network_namespace_inode: int
    owner_pid: int
    owner_start_ticks: int
    owner_uid: int
    owner_cgroup_inode: int
    local_host: str
    local_port: int
    peer_host: str
    peer_port: int

    def __post_init__(self) -> None:
        if self.schema != "pok-supervisor-socket-capture-identity-v1":
            raise ValueError("unknown socket capture identity schema")
        object.__setattr__(
            self,
            "capture_session_digest",
            _digest(self.capture_session_digest, "socket capture session"),
        )
        _strict_int(self.connection_index, "socket connection index")
        if self.connection_index > 1:
            raise ValueError("socket connection index must be 0 or 1")
        for name in (
            "socket_fd",
            "socket_inode",
            "socket_cookie",
            "network_namespace_inode",
            "owner_pid",
            "owner_start_ticks",
            "owner_uid",
            "owner_cgroup_inode",
        ):
            _strict_int(getattr(self, name), name, minimum=1)
        for name in ("local_port", "peer_port"):
            value = _strict_int(getattr(self, name), name, minimum=1)
            if value > 65_535:
                raise ValueError(f"{name} exceeds the TCP port range")
        for name in ("local_host", "peer_host"):
            try:
                normalized = str(ipaddress.ip_address(getattr(self, name)))
            except ValueError as exc:
                raise ValueError(f"{name} is not a canonical IP address") from exc
            if normalized != getattr(self, name):
                raise ValueError(f"{name} is not canonical")

    def digest(self) -> str:
        return canonical_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class DecisionEnforcementEvent:
    """One externally observed decision interval.

    ``action_raw_record_seq``, ``action_token_digest``, and
    ``action_sent_monotonic_ns`` describe only a parser-committed client token
    at its ingress boundary.  They are an all-or-none triple.  A fault-free
    decision must carry the triple; timeout must not.  Crash, resource,
    protocol, and infrastructure faults may be token-bearing (the fault
    happened after ingress) or tokenless (the supervisor adjudicated fold
    before any client token).  In particular, the peer ``fold`` emitted by
    fault adjudication is never represented as a client action.

    ``decision_close_*`` always identifies that server-to-peer relay boundary.
    It closes both normal and timeout decisions and therefore supplies the raw
    causal endpoint even when no client token exists.
    """

    schema: str
    decision_id: str
    capture_session_digest: str
    connection_index: int
    hand_index: int
    street: str
    decision_index: int
    request_raw_record_seq: int
    action_raw_record_seq: int | None
    decision_close_raw_record_seq: int
    request_token_digest: str
    action_token_digest: str | None
    requested_monotonic_ns: int
    worker_thawed_monotonic_ns: int
    complete_snapshot_monotonic_ns: int
    worker_frozen_monotonic_ns: int
    action_sent_monotonic_ns: int | None
    decision_close_monotonic_ns: int
    compute_budget_ms: int
    platform_timeout_ms: int
    selected_snapshot_digest: str
    fallback_was_ready_at_request: bool
    opponent_worker_frozen: bool
    hard_stop_fired: bool
    fault_kind: str
    fault_connection_index: int | None

    def __post_init__(self) -> None:
        if self.schema != DECISION_ENFORCEMENT_EVENT_SCHEMA:
            raise ValueError("unknown decision enforcement event schema")
        object.__setattr__(self, "decision_id", _digest(self.decision_id, "decision ID"))
        object.__setattr__(
            self,
            "capture_session_digest",
            _digest(self.capture_session_digest, "decision capture session"),
        )
        _strict_int(self.connection_index, "decision connection")
        if self.connection_index > 1:
            raise ValueError("decision connection must be 0 or 1")
        _strict_int(self.hand_index, "decision hand index")
        if self.hand_index > 69:
            raise ValueError("decision hand index must be in the 70-hand match")
        if self.street not in {"preflop", "flop", "turn", "river"}:
            raise ValueError("decision street is invalid")
        _strict_int(self.decision_index, "global decision index")
        _strict_int(self.request_raw_record_seq, "decision-open raw record sequence")
        _strict_int(
            self.decision_close_raw_record_seq,
            "decision-close raw record sequence",
        )
        if self.decision_close_raw_record_seq <= self.request_raw_record_seq:
            raise ValueError("decision-close raw record must follow its request")
        object.__setattr__(
            self,
            "request_token_digest",
            _digest(self.request_token_digest, "request_token_digest"),
        )
        allowed_fault_kinds = {
            "none",
            "crash",
            "timeout",
            "resource",
            "protocol",
            "infrastructure",
        }
        if self.fault_kind not in allowed_fault_kinds:
            raise ValueError("unknown per-decision fault kind")
        is_timeout = self.fault_kind == "timeout"
        action_fields = (
            self.action_raw_record_seq,
            self.action_token_digest,
            self.action_sent_monotonic_ns,
        )
        action_present = all(value is not None for value in action_fields)
        if any(value is not None for value in action_fields) and not action_present:
            raise ValueError(
                "client action token sequence, digest, and ingress must be all present or all null"
            )
        if is_timeout and action_present:
            raise ValueError(
                "timeout decision must not claim a client action token or ingress"
            )
        if self.fault_kind == "none" and not action_present:
            raise ValueError(
                "fault-free decision requires a client action token and ingress"
            )
        if action_present:
            _strict_int(self.action_raw_record_seq, "action raw record sequence")
            if not (
                self.request_raw_record_seq
                < self.action_raw_record_seq
                < self.decision_close_raw_record_seq
            ):
                raise ValueError(
                    "action raw record must lie between decision open and close records"
                )
            object.__setattr__(
                self,
                "action_token_digest",
                _digest(self.action_token_digest, "action_token_digest"),
            )
        expected_decision_id = canonical_digest(
            {
                "action_raw_record_seq": self.action_raw_record_seq,
                "action_token_digest": self.action_token_digest,
                "capture_session_digest": self.capture_session_digest,
                "connection_index": self.connection_index,
                "decision_close_raw_record_seq": self.decision_close_raw_record_seq,
                "decision_index": self.decision_index,
                "hand_index": self.hand_index,
                "request_raw_record_seq": self.request_raw_record_seq,
                "schema": DECISION_IDENTITY_SCHEMA,
                "street": self.street,
            }
        )
        if self.decision_id != expected_decision_id:
            raise ValueError("decision ID is not bound to its wire/capture location")
        for name in (
            "requested_monotonic_ns",
            "worker_thawed_monotonic_ns",
            "complete_snapshot_monotonic_ns",
            "worker_frozen_monotonic_ns",
            "decision_close_monotonic_ns",
        ):
            _strict_int(getattr(self, name), name, minimum=1)
        if self.action_sent_monotonic_ns is not None:
            _strict_int(
                self.action_sent_monotonic_ns,
                "client action token ingress monotonic timestamp",
                minimum=1,
            )
        _strict_int(self.compute_budget_ms, "decision compute budget", minimum=1)
        if self.compute_budget_ms > 54_000:
            raise ValueError("decision compute budget exceeds the 54 second hard stop")
        if self.platform_timeout_ms != 60_000:
            raise ValueError("decision platform timeout must be exactly 60 seconds")
        object.__setattr__(
            self,
            "selected_snapshot_digest",
            _digest(self.selected_snapshot_digest, "selected strategy snapshot"),
        )
        if self.fallback_was_ready_at_request is not True:
            raise ValueError("every decision must begin with a complete legal fallback")
        if self.opponent_worker_frozen is not True:
            raise ValueError("opponent-time pondering was not prevented")
        if type(self.hard_stop_fired) is not bool:
            raise ValueError("hard-stop marker must be boolean")
        if self.fault_kind == "none":
            if self.fault_connection_index is not None:
                raise ValueError("a fault-free decision cannot name a fault owner")
        elif self.fault_kind == "infrastructure":
            if self.fault_connection_index is not None:
                raise ValueError("an infrastructure fault cannot be charged to a candidate")
        elif self.fault_connection_index != self.connection_index:
            raise ValueError("a decision fault must be charged only to the acting connection")
        if not (
            self.requested_monotonic_ns
            <= self.worker_thawed_monotonic_ns
            <= self.worker_frozen_monotonic_ns
            <= self.decision_close_monotonic_ns
        ) or self.complete_snapshot_monotonic_ns > self.worker_frozen_monotonic_ns:
            raise ValueError("decision enforcement timestamps are not monotonic")
        if self.action_sent_monotonic_ns is not None and not (
            self.worker_frozen_monotonic_ns
            <= self.action_sent_monotonic_ns
            <= self.decision_close_monotonic_ns
        ):
            raise ValueError(
                "client action token ingress is outside the enforced decision interval"
            )
        compute_deadline = (
            self.requested_monotonic_ns + self.compute_budget_ms * 1_000_000
        )
        platform_deadline = (
            self.requested_monotonic_ns + self.platform_timeout_ms * 1_000_000
        )
        if self.worker_frozen_monotonic_ns > compute_deadline:
            raise ValueError("candidate worker exceeded the per-decision compute stop")
        if (
            self.action_sent_monotonic_ns is not None
            and self.action_sent_monotonic_ns > platform_deadline
        ):
            raise ValueError("client action token arrived after the platform timeout")
        if self.hard_stop_fired and self.worker_frozen_monotonic_ns != compute_deadline:
            raise ValueError("hard-stop event is not pinned to the compute deadline")
        if self.fault_kind == "timeout":
            if self.decision_close_monotonic_ns < platform_deadline:
                raise ValueError(
                    "timeout adjudication closed before the platform deadline"
                )
            if self.hard_stop_fired is not True:
                raise ValueError("a timeout must prove the compute hard stop fired")
        elif not action_present and self.decision_close_monotonic_ns >= platform_deadline:
            raise ValueError(
                "tokenless non-timeout fault closed at or after the platform deadline; "
                "timeout attribution takes precedence"
            )

    def digest(self) -> str:
        return canonical_digest(asdict(self))


def decision_trace_digest(events: Sequence[DecisionEnforcementEvent]) -> str:
    materialized = tuple(events)
    if len({event.decision_id for event in materialized}) != len(materialized):
        raise ValueError("decision enforcement trace repeats a decision ID")
    if any(
        left.decision_close_monotonic_ns > right.requested_monotonic_ns
        for left, right in zip(materialized, materialized[1:])
    ):
        raise ValueError("decision enforcement intervals overlap")
    if any(
        left.requested_monotonic_ns >= right.requested_monotonic_ns
        for left, right in zip(materialized, materialized[1:])
    ):
        raise ValueError("decision enforcement events are not strictly ordered")
    if materialized and tuple(event.decision_index for event in materialized) != tuple(
        range(len(materialized))
    ):
        raise ValueError("decision enforcement trace indices are not contiguous")
    if any(
        left.decision_close_raw_record_seq > right.request_raw_record_seq
        for left, right in zip(materialized, materialized[1:])
    ):
        raise ValueError("decision raw-wire record intervals overlap")
    if len({event.capture_session_digest for event in materialized}) > 1:
        raise ValueError("decision trace crosses capture sessions")
    return canonical_digest(
        {
            "decision_event_digests": tuple(event.digest() for event in materialized),
            "schema": "pok-decision-enforcement-trace-v2",
        }
    )


def decision_fault_trace_digest(
    events: Sequence[DecisionEnforcementEvent],
) -> str:
    """Digest the signed fault-owner facts used by retry attribution."""

    faults = tuple(event for event in events if event.fault_kind != "none")
    return canonical_digest(
        {
            "fault_event_digests": tuple(event.digest() for event in faults),
            "schema": "pok-supervisor-decision-fault-trace-v2",
        }
    )


@dataclass(frozen=True, slots=True)
class SupervisorLaunchAuthorization:
    """Prelaunch authorization; it deliberately contains no post-run evidence."""

    schema: str
    contract_digest: str
    readiness_attestation_digest: str
    control_session_digest: str
    request_nonce: str
    attempt_journal_scope_digest: str
    attempt_sequence: int
    previous_attempt_entry_digest: str
    leg_plan_digest: str
    leg_run_id: str
    profile_digest: str
    ordered_identity_digests: tuple[str, str]
    ordered_materialization_digests: tuple[str, str]
    ordered_launch_command_digests: tuple[str, str]
    ordered_base_environment_digests: tuple[str, str]
    ordered_launch_environment_digests: tuple[str, str]
    ordered_issuer_digests: tuple[str, str]
    ordered_execution_verifier_digests: tuple[str, str]
    ordered_policy_seeds: tuple[int, int]
    ordered_process_uids: tuple[int, int]
    issued_epoch_ms: int
    expires_epoch_ms: int
    signature_hex: str

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature_hex")
        return payload

    def payload_digest(self) -> str:
        return canonical_digest(self.unsigned_payload())

    def validate(
        self,
        contract: TrustedSupervisorContract,
        readiness: SupervisorAttestation,
    ) -> None:
        if self.schema != "pok-trusted-resource-supervisor-launch-authorization-v1":
            raise ValueError("unknown supervisor launch authorization schema")
        if self.contract_digest != contract.digest():
            raise ValueError("launch authorization belongs to another contract")
        if self.readiness_attestation_digest != readiness.payload_digest():
            raise ValueError("launch authorization lacks its readiness attestation")
        object.__setattr__(
            self,
            "control_session_digest",
            _digest(self.control_session_digest, "launch control session"),
        )
        if self.control_session_digest != readiness.control_session_digest:
            raise ValueError("launch authorization belongs to another control session")
        if (
            not isinstance(self.request_nonce, str)
            or len(self.request_nonce) < 32
            or len(self.request_nonce) > 256
            or self.request_nonce == readiness.nonce
        ):
            raise ValueError("launch authorization challenge is invalid or reused")
        _strict_int(self.attempt_sequence, "formal attempt sequence", minimum=1)
        for name in (
            "attempt_journal_scope_digest",
            "previous_attempt_entry_digest",
            "leg_plan_digest",
            "leg_run_id",
            "profile_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        digest_pairs = (
            self.ordered_identity_digests,
            self.ordered_materialization_digests,
            self.ordered_launch_command_digests,
            self.ordered_base_environment_digests,
            self.ordered_launch_environment_digests,
            self.ordered_issuer_digests,
            self.ordered_execution_verifier_digests,
        )
        if any(len(tuple(pair)) != 2 for pair in digest_pairs):
            raise ValueError("launch authorization digest fields require two slots")
        for pair in digest_pairs:
            for value in pair:
                _digest(value, "launch authorization digest")
        if self.ordered_identity_digests[0] == self.ordered_identity_digests[1]:
            raise ValueError("launch authorization requires two distinct identities")
        if len(tuple(self.ordered_policy_seeds)) != 2 or any(
            type(value) is not int or value < 0
            for value in self.ordered_policy_seeds
        ):
            raise ValueError("launch authorization requires two policy seeds")
        if tuple(self.ordered_process_uids) != contract.bot_uids_by_connection:
            raise ValueError("launch authorization did not allocate isolated bot UIDs")
        _strict_int(self.issued_epoch_ms, "launch issue time", minimum=1)
        _strict_int(self.expires_epoch_ms, "launch expiry", minimum=1)
        if not (
            readiness.issued_epoch_ms
            <= self.issued_epoch_ms
            <= self.expires_epoch_ms
            <= readiness.expires_epoch_ms
        ):
            raise ValueError("launch authorization is outside readiness validity")
        try:
            signature = bytes.fromhex(self.signature_hex)
        except (TypeError, ValueError) as exc:
            raise ValueError("launch authorization signature is not hexadecimal") from exc
        if len(signature) < 64 or len(signature) > 16_384:
            raise ValueError("launch authorization signature length is invalid")


@dataclass(frozen=True, slots=True)
class SupervisorLegReceipt:
    """Externally signed leg receipt; the in-process launcher cannot issue it."""

    schema: str
    contract_digest: str
    readiness_attestation_digest: str
    control_session_digest: str
    launch_authorization_digest: str
    request_nonce: str
    capture_challenge: str
    attempt_journal_scope_digest: str
    attempt_sequence: int
    previous_attempt_entry_digest: str
    capture_session_digest: str
    leg_plan_digest: str
    leg_run_id: str
    profile_digest: str
    ordered_identity_digests: tuple[str, str]
    ordered_materialization_digests: tuple[str, str]
    ordered_launch_command_digests: tuple[str, str]
    ordered_base_environment_digests: tuple[str, str]
    ordered_launch_environment_digests: tuple[str, str]
    ordered_policy_seeds: tuple[int, int]
    ordered_process_pids: tuple[int, int]
    ordered_process_group_ids: tuple[int, int]
    ordered_process_start_ticks: tuple[int, int]
    ordered_process_uids: tuple[int, int]
    ordered_cgroup_paths: tuple[str, str]
    ordered_cgroup_inodes: tuple[int, int]
    cgroup_mount_id: int
    ordered_socket_identities: tuple[SocketCaptureIdentity, SocketCaptureIdentity]
    raw_wire_digest: str
    wire_semantic_digest: str
    replay_digest: str
    execution_raw_record_digests: tuple[str, str]
    resource_raw_record_digests: tuple[str, str]
    ordered_issuer_digests: tuple[str, str]
    ordered_execution_verifier_digests: tuple[str, str]
    decision_events: tuple[DecisionEnforcementEvent, ...]
    decision_trace_digest: str
    cleanup_receipt_digest: str
    termination_kinds: tuple[str, str]
    no_pondering_verified: bool
    per_decision_hard_stop_verified: bool
    cleanup_empty_and_removed_verified: bool
    receipt_consumption_key: str
    consumption_ledger_entry_digest: str
    consumption_ledger_entry_inode: int
    issued_epoch_ms: int
    signature_hex: str

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature_hex")
        return payload

    def payload_digest(self) -> str:
        return canonical_digest(self.unsigned_payload())

    def validate(
        self,
        contract: TrustedSupervisorContract,
        readiness: SupervisorAttestation,
        launch: SupervisorLaunchAuthorization,
    ) -> None:
        if self.schema != "pok-trusted-resource-supervisor-leg-receipt-v3":
            raise ValueError("unknown supervisor leg receipt schema")
        if self.contract_digest != contract.digest():
            raise ValueError("supervisor leg receipt belongs to another contract")
        if self.readiness_attestation_digest != readiness.payload_digest():
            raise ValueError("supervisor leg receipt lacks its readiness attestation")
        object.__setattr__(
            self,
            "launch_authorization_digest",
            _digest(self.launch_authorization_digest, "launch authorization"),
        )
        if self.launch_authorization_digest != launch.payload_digest():
            raise ValueError("supervisor leg receipt belongs to another launch")
        object.__setattr__(
            self,
            "control_session_digest",
            _digest(self.control_session_digest, "supervisor leg control session"),
        )
        if self.control_session_digest != readiness.control_session_digest:
            raise ValueError("supervisor leg receipt belongs to another control session")
        if (
            not isinstance(self.request_nonce, str)
            or len(self.request_nonce) < 32
            or len(self.request_nonce) > 256
        ):
            raise ValueError("supervisor leg request nonce is invalid")
        if self.request_nonce != launch.request_nonce:
            raise ValueError("post-run receipt changed the prelaunch challenge")
        if (
            not isinstance(self.capture_challenge, str)
            or len(self.capture_challenge) < 32
            or len(self.capture_challenge) > 256
            or self.capture_challenge in {readiness.nonce, launch.request_nonce}
        ):
            raise ValueError("post-run capture challenge is invalid or reused")
        _strict_int(self.attempt_sequence, "formal attempt sequence", minimum=1)
        if self.attempt_sequence != launch.attempt_sequence:
            raise ValueError("post-run receipt changed the formal attempt sequence")
        object.__setattr__(
            self,
            "capture_session_digest",
            _digest(self.capture_session_digest, "capture session"),
        )
        for name in (
            "leg_plan_digest",
            "leg_run_id",
            "profile_digest",
            "attempt_journal_scope_digest",
            "previous_attempt_entry_digest",
            "decision_trace_digest",
            "cleanup_receipt_digest",
            "raw_wire_digest",
            "wire_semantic_digest",
            "replay_digest",
            "receipt_consumption_key",
            "consumption_ledger_entry_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        digest_pairs = (
            self.ordered_identity_digests,
            self.ordered_materialization_digests,
            self.ordered_launch_command_digests,
            self.ordered_base_environment_digests,
            self.ordered_launch_environment_digests,
            self.execution_raw_record_digests,
            self.resource_raw_record_digests,
            self.ordered_issuer_digests,
            self.ordered_execution_verifier_digests,
        )
        if any(len(tuple(pair)) != 2 for pair in digest_pairs):
            raise ValueError("supervisor leg ordered digest fields require two slots")
        for pair in digest_pairs:
            for value in pair:
                _digest(value, "supervisor leg digest")
        for pair_name in (
            "ordered_process_pids",
            "ordered_process_group_ids",
            "ordered_process_start_ticks",
            "ordered_process_uids",
            "ordered_cgroup_inodes",
        ):
            pair = tuple(getattr(self, pair_name))
            if len(pair) != 2:
                raise ValueError(f"{pair_name} requires two slots")
            for value in pair:
                _strict_int(value, pair_name, minimum=1)
        policy_seeds = tuple(self.ordered_policy_seeds)
        if len(policy_seeds) != 2:
            raise ValueError("ordered_policy_seeds requires two slots")
        for value in policy_seeds:
            _strict_int(value, "ordered policy seed")
        if tuple(self.ordered_process_uids) != contract.bot_uids_by_connection:
            raise ValueError("supervisor leg did not use the isolated candidate UIDs")
        paths = tuple(self.ordered_cgroup_paths)
        if (
            len(paths) != 2
            or paths[0] == paths[1]
            or any(not path.startswith("/sys/fs/cgroup/") for path in paths)
        ):
            raise ValueError("supervisor leg cgroup identities are invalid")
        _strict_int(self.cgroup_mount_id, "supervisor leg cgroup mount", minimum=1)
        sockets = tuple(self.ordered_socket_identities)
        if (
            len(sockets) != 2
            or tuple(item.connection_index for item in sockets) != (0, 1)
            or any(
                item.capture_session_digest != self.capture_session_digest
                for item in sockets
            )
            or len({item.socket_cookie for item in sockets}) != 2
            or len({item.socket_inode for item in sockets}) != 2
            or len({item.network_namespace_inode for item in sockets}) != 2
        ):
            raise ValueError(
                "post-run receipt has invalid or non-isolated ordered socket identities"
            )
        if (
            tuple(item.owner_pid for item in sockets)
            != tuple(self.ordered_process_pids)
            or tuple(item.owner_start_ticks for item in sockets)
            != tuple(self.ordered_process_start_ticks)
            or tuple(item.owner_uid for item in sockets)
            != tuple(self.ordered_process_uids)
            or tuple(item.owner_cgroup_inode for item in sockets)
            != tuple(self.ordered_cgroup_inodes)
        ):
            raise ValueError(
                "post-run socket identities are not owned by the signed candidate processes"
            )
        object.__setattr__(self, "ordered_socket_identities", sockets)
        events = tuple(self.decision_events)
        object.__setattr__(self, "decision_events", events)
        if not events or decision_trace_digest(events) != self.decision_trace_digest:
            raise ValueError("supervisor leg decision trace is empty or stale")
        if any(
            event.capture_session_digest != self.capture_session_digest
            for event in events
        ):
            raise ValueError("decision trace belongs to another capture session")
        if (
            self.no_pondering_verified is not True
            or self.per_decision_hard_stop_verified is not True
            or self.cleanup_empty_and_removed_verified is not True
        ):
            raise ValueError("supervisor leg lacks mandatory isolation/cleanup proof")
        kinds = tuple(self.termination_kinds)
        if len(kinds) != 2 or any(
            kind not in {"normal", "crash", "timeout", "resource", "protocol", "infrastructure"}
            for kind in kinds
        ):
            raise ValueError("supervisor leg termination kinds are invalid")
        if sum(kind == "timeout" for kind in kinds) > 1:
            raise ValueError("one per-decision timeout cannot be charged to both candidates")
        candidate_faults = {
            (event.fault_connection_index, event.fault_kind)
            for event in events
            if event.fault_connection_index is not None
        }
        for connection_index, kind in enumerate(kinds):
            if kind in {"crash", "timeout", "resource", "protocol"} and (
                connection_index,
                kind,
            ) not in candidate_faults:
                raise ValueError(
                    "candidate termination lacks a same-decision fault-owner record"
                )
            if kind == "normal" and any(
                owner == connection_index for owner, _fault in candidate_faults
            ):
                raise ValueError("a normal candidate cannot own a decision fault")
        if any(kind == "infrastructure" for kind in kinds) and not any(
            event.fault_kind == "infrastructure" for event in events
        ):
            raise ValueError("infrastructure termination lacks an uncharged fault record")
        _strict_int(self.issued_epoch_ms, "supervisor leg issue time", minimum=1)
        _strict_int(
            self.consumption_ledger_entry_inode,
            "consumption ledger entry inode",
            minimum=1,
        )
        if self.issued_epoch_ms < launch.issued_epoch_ms:
            raise ValueError("supervisor leg receipt predates launch authority")
        launch_fields = {
            "leg_plan_digest": self.leg_plan_digest,
            "leg_run_id": self.leg_run_id,
            "profile_digest": self.profile_digest,
            "ordered_identity_digests": tuple(self.ordered_identity_digests),
            "ordered_materialization_digests": tuple(
                self.ordered_materialization_digests
            ),
            "ordered_launch_command_digests": tuple(
                self.ordered_launch_command_digests
            ),
            "ordered_base_environment_digests": tuple(
                self.ordered_base_environment_digests
            ),
            "ordered_launch_environment_digests": tuple(
                self.ordered_launch_environment_digests
            ),
            "ordered_issuer_digests": tuple(self.ordered_issuer_digests),
            "ordered_execution_verifier_digests": tuple(
                self.ordered_execution_verifier_digests
            ),
            "ordered_policy_seeds": tuple(self.ordered_policy_seeds),
            "ordered_process_uids": tuple(self.ordered_process_uids),
            "attempt_sequence": self.attempt_sequence,
            "attempt_journal_scope_digest": self.attempt_journal_scope_digest,
            "previous_attempt_entry_digest": self.previous_attempt_entry_digest,
        }
        for name, value in launch_fields.items():
            if getattr(launch, name) != value:
                raise ValueError(f"post-run receipt changed prelaunch field: {name}")
        expected_consumption_key = canonical_digest(
            {
                "capture_challenge": self.capture_challenge,
                "capture_session_digest": self.capture_session_digest,
                "control_session_digest": self.control_session_digest,
                "attempt_sequence": self.attempt_sequence,
                "attempt_journal_scope_digest": self.attempt_journal_scope_digest,
                "launch_authorization_digest": self.launch_authorization_digest,
                "leg_run_id": self.leg_run_id,
                "previous_attempt_entry_digest": self.previous_attempt_entry_digest,
                "raw_wire_digest": self.raw_wire_digest,
                "replay_digest": self.replay_digest,
                "schema": "pok-supervisor-leg-receipt-consumption-key-v1",
                "wire_semantic_digest": self.wire_semantic_digest,
            }
        )
        if self.receipt_consumption_key != expected_consumption_key:
            raise ValueError("supervisor leg receipt has a stale consumption key")
        try:
            signature = bytes.fromhex(self.signature_hex)
        except (TypeError, ValueError) as exc:
            raise ValueError("supervisor leg signature is not hexadecimal") from exc
        if len(signature) < 64 or len(signature) > 16_384:
            raise ValueError("supervisor leg signature length is invalid")


@dataclass(frozen=True, slots=True)
class SupervisorConsumptionLedgerEntry:
    """Root-owned no-clobber row bound by a signed leg receipt.

    The row is deliberately not self-referential: the signed leg receipt binds
    this row's digest and inode, while the row binds every stable post-run
    identity needed to reject a different receipt under the same consumption
    key.  A downstream formal-result ledger must still reject a repeated key;
    process-local sets are only defense in depth.
    """

    schema: str
    contract_digest: str
    readiness_attestation_digest: str
    control_session_digest: str
    attempt_journal_scope_digest: str
    attempt_sequence: int
    previous_attempt_entry_digest: str
    launch_authorization_digest: str
    leg_run_id: str
    capture_session_digest: str
    receipt_consumption_key: str
    raw_wire_digest: str
    wire_semantic_digest: str
    replay_digest: str
    ordered_socket_identity_digests: tuple[str, str]
    decision_trace_digest: str
    cleanup_receipt_digest: str
    termination_kinds: tuple[str, str]
    receipt_issued_epoch_ms: int

    def validate(
        self,
        contract: TrustedSupervisorContract,
        readiness: SupervisorAttestation,
        launch: SupervisorLaunchAuthorization,
        receipt: SupervisorLegReceipt,
    ) -> None:
        if self.schema != "pok-supervisor-consumption-ledger-entry-v1":
            raise ValueError("unknown supervisor consumption ledger schema")
        digest_fields = (
            "contract_digest",
            "readiness_attestation_digest",
            "control_session_digest",
            "attempt_journal_scope_digest",
            "previous_attempt_entry_digest",
            "launch_authorization_digest",
            "leg_run_id",
            "capture_session_digest",
            "receipt_consumption_key",
            "raw_wire_digest",
            "wire_semantic_digest",
            "replay_digest",
            "decision_trace_digest",
            "cleanup_receipt_digest",
        )
        for name in digest_fields:
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        _strict_int(self.attempt_sequence, "ledger attempt sequence", minimum=1)
        _strict_int(self.receipt_issued_epoch_ms, "ledger receipt issue time", minimum=1)
        socket_digests = tuple(self.ordered_socket_identity_digests)
        if len(socket_digests) != 2:
            raise ValueError("ledger socket identity digests require two slots")
        for value in socket_digests:
            _digest(value, "ledger socket identity")
        object.__setattr__(self, "ordered_socket_identity_digests", socket_digests)
        kinds = tuple(self.termination_kinds)
        if len(kinds) != 2:
            raise ValueError("ledger termination kinds require two slots")
        object.__setattr__(self, "termination_kinds", kinds)
        expected = {
            "contract_digest": contract.digest(),
            "readiness_attestation_digest": readiness.payload_digest(),
            "control_session_digest": receipt.control_session_digest,
            "attempt_journal_scope_digest": receipt.attempt_journal_scope_digest,
            "attempt_sequence": receipt.attempt_sequence,
            "previous_attempt_entry_digest": receipt.previous_attempt_entry_digest,
            "launch_authorization_digest": launch.payload_digest(),
            "leg_run_id": receipt.leg_run_id,
            "capture_session_digest": receipt.capture_session_digest,
            "receipt_consumption_key": receipt.receipt_consumption_key,
            "raw_wire_digest": receipt.raw_wire_digest,
            "wire_semantic_digest": receipt.wire_semantic_digest,
            "replay_digest": receipt.replay_digest,
            "ordered_socket_identity_digests": tuple(
                item.digest() for item in receipt.ordered_socket_identities
            ),
            "decision_trace_digest": receipt.decision_trace_digest,
            "cleanup_receipt_digest": receipt.cleanup_receipt_digest,
            "termination_kinds": tuple(receipt.termination_kinds),
            "receipt_issued_epoch_ms": receipt.issued_epoch_ms,
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"consumption ledger entry changed signed field: {name}")

    def digest(self) -> str:
        return canonical_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class SupervisorAttemptJournalEntry:
    """Externally signed terminal row for every authorized launch attempt."""

    schema: str
    contract_digest: str
    readiness_attestation_digest: str
    control_session_digest: str
    attempt_journal_scope_digest: str
    attempt_sequence: int
    previous_attempt_entry_digest: str
    launch_authorization_digest: str
    leg_plan_digest: str
    leg_run_id: str
    terminal_state: str
    supervisor_leg_receipt_digest: str | None
    capture_session_digest: str | None
    receipt_consumption_key: str | None
    cleanup_receipt_digest: str | None
    raw_wire_digest: str | None
    wire_semantic_digest: str | None
    replay_digest: str | None
    replay_verification_digest: str | None
    issued_epoch_ms: int
    signature_hex: str

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature_hex")
        return payload

    def payload_digest(self) -> str:
        return canonical_digest(self.unsigned_payload())

    def validate(
        self,
        contract: TrustedSupervisorContract,
        launch: SupervisorLaunchAuthorization,
    ) -> None:
        if self.schema != "pok-supervisor-attempt-journal-entry-v1":
            raise ValueError("unknown supervisor attempt journal entry schema")
        for name in (
            "contract_digest",
            "readiness_attestation_digest",
            "control_session_digest",
            "attempt_journal_scope_digest",
            "previous_attempt_entry_digest",
            "launch_authorization_digest",
            "leg_plan_digest",
            "leg_run_id",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        _strict_int(self.attempt_sequence, "journal attempt sequence", minimum=1)
        _strict_int(self.issued_epoch_ms, "journal entry issue time", minimum=1)
        for name in (
            "supervisor_leg_receipt_digest",
            "capture_session_digest",
            "receipt_consumption_key",
            "cleanup_receipt_digest",
            "raw_wire_digest",
            "wire_semantic_digest",
            "replay_digest",
            "replay_verification_digest",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _digest(value, name))
        if self.terminal_state not in {
            "completed",
            "launch_failed",
            "capture_failed",
            "cleanup_failed",
            "infrastructure_failed",
            "aborted",
        }:
            raise ValueError("unknown supervisor attempt terminal state")
        expected = {
            "contract_digest": contract.digest(),
            "readiness_attestation_digest": launch.readiness_attestation_digest,
            "control_session_digest": launch.control_session_digest,
            "attempt_journal_scope_digest": launch.attempt_journal_scope_digest,
            "attempt_sequence": launch.attempt_sequence,
            "previous_attempt_entry_digest": launch.previous_attempt_entry_digest,
            "launch_authorization_digest": launch.payload_digest(),
            "leg_plan_digest": launch.leg_plan_digest,
            "leg_run_id": launch.leg_run_id,
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"attempt journal entry changed launch field: {name}")
        postrun = (
            self.supervisor_leg_receipt_digest,
            self.capture_session_digest,
            self.receipt_consumption_key,
            self.cleanup_receipt_digest,
            self.raw_wire_digest,
            self.wire_semantic_digest,
            self.replay_digest,
            self.replay_verification_digest,
        )
        if self.terminal_state == "completed" and any(value is None for value in postrun):
            raise ValueError(
                "completed attempt journal row lacks post-run/replay-verification binding"
            )
        if self.terminal_state == "launch_failed" and any(
            value is not None for value in postrun
        ):
            raise ValueError("launch-failed journal row cannot claim post-run evidence")
        try:
            signature = bytes.fromhex(self.signature_hex)
        except (TypeError, ValueError) as exc:
            raise ValueError("attempt journal signature is not hexadecimal") from exc
        if len(signature) < 64 or len(signature) > 16_384:
            raise ValueError("attempt journal signature length is invalid")


@dataclass(frozen=True, slots=True)
class SupervisorAttemptJournalSeal:
    """Externally signed closed head proving a complete attempt-chain prefix."""

    schema: str
    contract_digest: str
    attempt_journal_scope_digest: str
    first_attempt_sequence: int
    last_attempt_sequence: int
    entry_count: int
    first_previous_entry_digest: str
    head_entry_digest: str
    ordered_entry_chain_digest: str
    closed: bool
    issued_epoch_ms: int
    signature_hex: str

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature_hex")
        return payload

    def payload_digest(self) -> str:
        return canonical_digest(self.unsigned_payload())

    def validate(
        self,
        contract: TrustedSupervisorContract,
        entries: Sequence[SupervisorAttemptJournalEntry],
        *,
        expected_scope_digest: str,
    ) -> None:
        if self.schema != "pok-supervisor-attempt-journal-seal-v1":
            raise ValueError("unknown supervisor attempt journal seal schema")
        for name in (
            "contract_digest",
            "attempt_journal_scope_digest",
            "first_previous_entry_digest",
            "head_entry_digest",
            "ordered_entry_chain_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        scope = _digest(expected_scope_digest, "expected attempt journal scope")
        materialized = tuple(entries)
        if not materialized:
            raise ValueError("attempt journal seal cannot close an empty scope")
        if self.contract_digest != contract.digest() or self.attempt_journal_scope_digest != scope:
            raise ValueError("attempt journal seal belongs to another contract/scope")
        if self.closed is not True:
            raise ValueError("attempt journal scope is not closed")
        for value, name in (
            (self.first_attempt_sequence, "first attempt sequence"),
            (self.last_attempt_sequence, "last attempt sequence"),
            (self.entry_count, "attempt journal entry count"),
            (self.issued_epoch_ms, "attempt journal seal issue time"),
        ):
            _strict_int(value, name, minimum=1)
        if any(entry.attempt_journal_scope_digest != scope for entry in materialized):
            raise ValueError("attempt journal entries cross scopes")
        sequences = tuple(entry.attempt_sequence for entry in materialized)
        if sequences[0] != 1 or materialized[0].previous_attempt_entry_digest != "0" * 64:
            raise ValueError(
                "attempt journal scope must begin at sequence 1 and the zero genesis"
            )
        if sequences != tuple(range(sequences[0], sequences[0] + len(sequences))):
            raise ValueError("attempt journal sequence is not contiguous")
        digests = tuple(entry.payload_digest() for entry in materialized)
        if any(
            right.previous_attempt_entry_digest != left_digest
            for left_digest, right in zip(digests, materialized[1:])
        ):
            raise ValueError("attempt journal previous-entry chain is broken")
        expected_chain = canonical_digest(
            {
                "attempt_journal_scope_digest": scope,
                "entry_payload_digests": digests,
                "schema": "pok-supervisor-attempt-journal-chain-v1",
            }
        )
        expected = {
            "first_attempt_sequence": sequences[0],
            "last_attempt_sequence": sequences[-1],
            "entry_count": len(materialized),
            "first_previous_entry_digest": materialized[0].previous_attempt_entry_digest,
            "head_entry_digest": digests[-1],
            "ordered_entry_chain_digest": expected_chain,
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"attempt journal seal has stale field: {name}")
        if self.issued_epoch_ms < max(entry.issued_epoch_ms for entry in materialized):
            raise ValueError("attempt journal seal predates a terminal entry")
        try:
            signature = bytes.fromhex(self.signature_hex)
        except (TypeError, ValueError) as exc:
            raise ValueError("attempt journal seal signature is not hexadecimal") from exc
        if len(signature) < 64 or len(signature) > 16_384:
            raise ValueError("attempt journal seal signature length is invalid")


def required_controllers_digest() -> str:
    return canonical_digest(
        {
            "cgroup_version": 2,
            "controllers": list(REQUIRED_CONTROLLERS),
            "required_child_files": list(REQUIRED_CHILD_FILES),
            "schema": "pok-cgroup-controller-contract-v1",
        }
    )


def current_enforcer_digest() -> str:
    source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).digest()
    return hashlib.sha256(
        RESOURCE_ENFORCER_ALGORITHM_SPEC.encode("ascii") + b"\x00" + source_sha256
    ).hexdigest()


def default_thread_environment(cpu_threads: int) -> dict[str, str]:
    cpu_threads = _strict_int(cpu_threads, "CPU thread count", minimum=1)
    value = str(cpu_threads)
    return {
        "MKL_DYNAMIC": "FALSE",
        "MKL_NUM_THREADS": value,
        "NUMBA_NUM_THREADS": value,
        "NUMEXPR_NUM_THREADS": value,
        "OMP_DYNAMIC": "FALSE",
        "OMP_NUM_THREADS": value,
        "OPENBLAS_NUM_THREADS": value,
        "RAYON_NUM_THREADS": value,
        "TOKENIZERS_PARALLELISM": "false",
        "VECLIB_MAXIMUM_THREADS": value,
    }


def thread_environment_digest(environment: Mapping[str, str]) -> str:
    normalized = _normalize_environment(environment, exact_keys=THREAD_ENVIRONMENT_KEYS)
    return canonical_digest(
        {"environment": dict(normalized), "schema": "pok-thread-environment-v1"}
    )


def launch_command_digest(argv: Sequence[str], cwd: str | os.PathLike[str]) -> str:
    normalized = _normalize_argv(argv)
    directory = str(Path(cwd).resolve())
    return canonical_digest(
        {"argv": list(normalized), "cwd": directory, "schema": "pok-launch-command-v1"}
    )


def launch_environment_digest(environment: Mapping[str, str]) -> str:
    normalized = _normalize_environment(environment)
    return canonical_digest(
        {"environment": dict(normalized), "schema": "pok-launch-environment-v1"}
    )


def _normalize_environment(
    environment: Mapping[str, str], *, exact_keys: Sequence[str] | None = None
) -> tuple[tuple[str, str], ...]:
    if not isinstance(environment, Mapping):
        raise ValueError("environment must be a mapping")
    normalized: list[tuple[str, str]] = []
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
        ):
            raise ValueError("environment contains an invalid key or value")
        normalized.append((key, value))
    normalized.sort()
    if len({key for key, _ in normalized}) != len(normalized):
        raise ValueError("environment contains duplicate keys")
    if exact_keys is not None and tuple(key for key, _ in normalized) != tuple(
        sorted(exact_keys)
    ):
        raise ValueError("thread environment keys differ from the frozen contract")
    return tuple(normalized)


def _normalize_argv(argv: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(argv)
    if not normalized or any(
        not isinstance(item, str) or not item or "\x00" in item for item in normalized
    ):
        raise ValueError("launch argv must contain non-empty NUL-free strings")
    executable = Path(normalized[0])
    if not executable.is_absolute():
        raise ValueError("formal executable path must be absolute")
    return (str(executable.resolve()), *normalized[1:])


def _format_cpuset(cpus: Sequence[int]) -> str:
    values = sorted(set(cpus))
    if not values or len(values) != len(tuple(cpus)) or any(
        type(value) is not int or value < 0 for value in values
    ):
        raise ValueError("CPU set must be non-empty, unique nonnegative integers")
    ranges: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _parse_cpuset(value: str) -> tuple[int, ...]:
    values: list[int] = []
    for part in value.strip().split(","):
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError("invalid cpuset range")
            values.extend(range(start, end + 1))
        else:
            values.append(int(part))
    if not values or len(set(values)) != len(values) or any(value < 0 for value in values):
        raise ValueError("invalid cpuset")
    return tuple(sorted(values))


@dataclass(frozen=True, slots=True)
class EnforcementProfile:
    profile_digest: str
    cpu_affinity_by_connection: tuple[tuple[int, ...], tuple[int, ...]]
    cpu_threads_per_connection: int
    cpu_quota_us: int
    cpu_period_us: int
    max_tasks_per_connection: int
    ram_limit_bytes_per_connection: int
    swap_limit_bytes_per_connection: int
    gpu_devices_by_connection: tuple[tuple[str, ...], tuple[str, ...]]
    vram_limit_bytes_per_connection: int
    decision_budget_ms: int
    platform_action_timeout_ms: int
    match_wall_timeout_ms: int
    action_send_delay_ms: int
    concurrent_matches: int
    pondering_allowed: bool
    thread_environment: tuple[tuple[str, str], ...]
    thread_environment_digest: str
    enforcer_digest: str
    controllers_digest: str

    @classmethod
    def from_evaluation(
        cls, profile: object, thread_environment: Mapping[str, str]
    ) -> "EnforcementProfile":
        normalized_environment = _normalize_environment(
            thread_environment, exact_keys=THREAD_ENVIRONMENT_KEYS
        )
        result = cls(
            profile_digest=profile.digest(),
            cpu_affinity_by_connection=tuple(
                tuple(slot) for slot in profile.cpu_affinity_by_connection
            ),
            cpu_threads_per_connection=profile.cpu_threads_per_connection,
            cpu_quota_us=profile.cpu_quota_us,
            cpu_period_us=profile.cpu_period_us,
            max_tasks_per_connection=profile.max_tasks_per_connection,
            ram_limit_bytes_per_connection=profile.ram_limit_bytes_per_connection,
            swap_limit_bytes_per_connection=profile.swap_limit_bytes_per_connection,
            gpu_devices_by_connection=tuple(
                tuple(slot) for slot in profile.gpu_devices_by_connection
            ),
            vram_limit_bytes_per_connection=profile.vram_limit_bytes_per_connection,
            decision_budget_ms=profile.decision_budget_ms,
            platform_action_timeout_ms=profile.platform_action_timeout_ms,
            match_wall_timeout_ms=profile.match_wall_timeout_ms,
            action_send_delay_ms=profile.action_send_delay_ms,
            concurrent_matches=profile.concurrent_matches,
            pondering_allowed=profile.pondering_allowed,
            thread_environment=normalized_environment,
            thread_environment_digest=profile.thread_environment_digest,
            enforcer_digest=profile.enforcer_digest,
            controllers_digest=profile.cgroup_controllers_digest,
        )
        result.validate()
        return result

    def validate(self) -> None:
        _digest(self.profile_digest, "profile digest")
        slots = tuple(tuple(slot) for slot in self.cpu_affinity_by_connection)
        if len(slots) != 2 or set(slots[0]) & set(slots[1]):
            raise ValueError("formal cgroup CPU slots must be two disjoint sets")
        for slot in slots:
            _format_cpuset(slot)
        _strict_int(self.cpu_threads_per_connection, "CPU threads", minimum=1)
        if any(self.cpu_threads_per_connection > len(slot) for slot in slots):
            raise ValueError("CPU thread count exceeds a frozen affinity slot")
        _strict_int(self.cpu_quota_us, "CPU quota", minimum=1)
        _strict_int(self.cpu_period_us, "CPU period", minimum=1)
        if self.cpu_quota_us > self.cpu_period_us * self.cpu_threads_per_connection:
            raise ValueError("CPU quota exceeds the frozen per-bot core envelope")
        _strict_int(self.max_tasks_per_connection, "task limit", minimum=1)
        if self.max_tasks_per_connection < self.cpu_threads_per_connection:
            raise ValueError("task limit is smaller than the frozen thread count")
        _strict_int(self.ram_limit_bytes_per_connection, "memory limit", minimum=1)
        if self.swap_limit_bytes_per_connection != 0:
            raise ValueError("formal cgroup enforcement requires memory.swap.max=0")
        _strict_int(self.decision_budget_ms, "decision budget", minimum=1)
        if self.decision_budget_ms > 54_000:
            raise ValueError("formal decision budget exceeds the hard compute stop")
        if self.platform_action_timeout_ms != 60_000:
            raise ValueError("formal platform action timeout must be 60000 ms")
        if self.decision_budget_ms >= self.platform_action_timeout_ms:
            raise ValueError("decision budget must stop before the platform timeout")
        _strict_int(self.match_wall_timeout_ms, "match wall timeout", minimum=1)
        if self.match_wall_timeout_ms <= self.platform_action_timeout_ms:
            raise ValueError("match wall timeout cannot be a decision timeout")
        _strict_int(self.action_send_delay_ms, "action send delay")
        if type(self.concurrent_matches) is not int or self.concurrent_matches != 1:
            raise ValueError("formal resource enforcement is globally sequential")
        if type(self.pondering_allowed) is not bool or self.pondering_allowed:
            raise ValueError("formal resource enforcement forbids pondering")
        if (
            self.enforcer_digest != RESOURCE_ENFORCER_DIGEST
            or current_enforcer_digest() != RESOURCE_ENFORCER_DIGEST
        ):
            raise ValueError("resource profile does not pin this enforcer algorithm")
        if self.controllers_digest != required_controllers_digest():
            raise ValueError("resource profile controller contract differs")
        if thread_environment_digest(dict(self.thread_environment)) != self.thread_environment_digest:
            raise ValueError("thread environment digest differs from the profile")
        if dict(self.thread_environment) != default_thread_environment(
            self.cpu_threads_per_connection
        ):
            raise ValueError("thread environment values differ from the frozen thread count")
        gpu_slots = tuple(tuple(slot) for slot in self.gpu_devices_by_connection)
        if (
            len(gpu_slots) != 2
            or any(len(set(slot)) != len(slot) for slot in gpu_slots)
            or set(gpu_slots[0]) & set(gpu_slots[1])
        ):
            raise ValueError("formal GPU slots must be two disjoint sets")
        if any(
            not isinstance(device, str) or not device
            for slot in gpu_slots
            for device in slot
        ):
            raise ValueError("formal GPU slots contain an invalid device")
        _strict_int(self.vram_limit_bytes_per_connection, "VRAM limit")
        if any(self.gpu_devices_by_connection) or self.vram_limit_bytes_per_connection:
            raise FormalEnforcementUnavailable(
                "CUDA visibility alone cannot enforce a formal VRAM limit; no GPU backend is installed"
            )
        if self.profile_digest != self.computed_profile_digest():
            raise ValueError("resource profile digest does not bind the actual configuration")

    def evaluation_profile_payload(self) -> dict[str, Any]:
        """Return the exact payload hashed by ``evaluation.ResourceProfile``."""

        return {
            "action_send_delay_ms": self.action_send_delay_ms,
            "cgroup_controllers_digest": self.controllers_digest,
            "concurrent_matches": self.concurrent_matches,
            "cpu_affinity_by_connection": self.cpu_affinity_by_connection,
            "cpu_period_us": self.cpu_period_us,
            "cpu_quota_us": self.cpu_quota_us,
            "cpu_threads_per_connection": self.cpu_threads_per_connection,
            "decision_budget_ms": self.decision_budget_ms,
            "enforcer_digest": self.enforcer_digest,
            "gpu_devices_by_connection": self.gpu_devices_by_connection,
            "match_wall_timeout_ms": self.match_wall_timeout_ms,
            "max_tasks_per_connection": self.max_tasks_per_connection,
            "platform_action_timeout_ms": self.platform_action_timeout_ms,
            "pondering_allowed": self.pondering_allowed,
            "ram_limit_bytes_per_connection": self.ram_limit_bytes_per_connection,
            "swap_limit_bytes_per_connection": self.swap_limit_bytes_per_connection,
            "thread_environment_digest": self.thread_environment_digest,
            "vram_limit_bytes_per_connection": self.vram_limit_bytes_per_connection,
        }

    def computed_profile_digest(self) -> str:
        payload = json.dumps(
            self.evaluation_profile_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _relative_manifest_path(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} is invalid")
    path = Path(value)
    if path.is_absolute() or value != path.as_posix() or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ValueError(f"{name} must be a normalized relative path")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactFileRecord:
    relative_path: str
    size_bytes: int
    mode: int
    sha256: str
    role: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relative_path",
            _relative_manifest_path(self.relative_path, "artifact file path"),
        )
        _strict_int(self.size_bytes, "artifact file size")
        if type(self.mode) is not int or self.mode < 0 or self.mode > 0o777:
            raise ValueError("artifact file mode must be a permission mask")
        object.__setattr__(self, "sha256", _digest(self.sha256, "artifact file"))
        if self.role not in {*ARTIFACT_ROLES, "other"}:
            raise ValueError("artifact file has an unknown content role")

    def content_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


def _role_manifest_digest(
    records: Sequence[ArtifactFileRecord], role: str
) -> str:
    if role not in ARTIFACT_ROLES:
        raise ValueError("unknown artifact role")
    return canonical_digest(
        {
            "files": [
                record.content_payload()
                for record in records
                if record.role == role
            ],
            "role": role,
            "schema": "pok-artifact-role-manifest-v1",
        }
    )


def _sealed_tree_manifest_digest(records: Sequence[ArtifactFileRecord]) -> str:
    return canonical_digest(
        {
            "files": [
                {**record.content_payload(), "role": record.role}
                for record in records
            ],
            "schema": "pok-sealed-regular-file-tree-v1",
        }
    )


def _artifact_identity_digest(
    *,
    sealed_tree_manifest_digest: str,
    launch_contract_digest: str,
    launch_command_digest_value: str,
    base_environment_digest: str,
    model_digest: str,
    config_digest: str,
    action_set_digest: str,
    dependency_digest: str,
    runtime_digest: str,
) -> str:
    # This payload intentionally matches evaluation.ArtifactIdentity exactly.
    payload = {
            "action_set_digest": action_set_digest,
            "base_environment_digest": base_environment_digest,
            "config_digest": config_digest,
            "dependency_digest": dependency_digest,
            "launch_command_digest": launch_command_digest_value,
            "launch_contract_digest": launch_contract_digest,
            "model_digest": model_digest,
            "runtime_digest": runtime_digest,
            "sealed_tree_manifest_digest": sealed_tree_manifest_digest,
        }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactMaterializationExpectation:
    artifact_root: str
    executable_relative_path: str
    cwd_relative_path: str
    argv_tail: tuple[str, ...]
    base_environment: tuple[tuple[str, str], ...]
    files: tuple[ArtifactFileRecord, ...]
    sealed_tree_manifest_digest: str
    launch_contract_digest: str
    launch_command_digest: str
    base_environment_digest: str
    model_digest: str
    config_digest: str
    action_set_digest: str
    dependency_digest: str
    runtime_digest: str
    identity_digest: str
    formal_readonly_cas_required: bool

    def __post_init__(self) -> None:
        root = _strict_absolute_path(self.artifact_root, "artifact root")
        if str(root) != self.artifact_root:
            raise ValueError("artifact root is not normalized")
        object.__setattr__(
            self,
            "executable_relative_path",
            _relative_manifest_path(
                self.executable_relative_path, "artifact executable"
            ),
        )
        cwd = self.cwd_relative_path
        if cwd != ".":
            _relative_manifest_path(cwd, "artifact cwd")
        argv_tail = tuple(self.argv_tail)
        if any(not isinstance(value, str) or not value or "\x00" in value for value in argv_tail):
            raise ValueError("artifact argv tail is invalid")
        object.__setattr__(self, "argv_tail", argv_tail)
        environment = _normalize_environment(dict(self.base_environment))
        if environment != tuple(self.base_environment):
            raise ValueError("artifact base environment must be sorted and unique")
        files = tuple(self.files)
        if not files or tuple(sorted(files, key=lambda item: item.relative_path)) != files:
            raise ValueError("artifact file manifest must be non-empty and sorted")
        if len({record.relative_path for record in files}) != len(files):
            raise ValueError("artifact file manifest contains duplicate paths")
        executable = next(
            (
                record
                for record in files
                if record.relative_path == self.executable_relative_path
            ),
            None,
        )
        if executable is None or not executable.mode & 0o100:
            raise ValueError("artifact executable is absent or not owner-executable")
        object.__setattr__(self, "files", files)
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
            "identity_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if type(self.formal_readonly_cas_required) is not bool:
            raise ValueError("formal CAS marker must be boolean")
        self._verify_digests()

    def _verify_digests(self) -> None:
        tree = _sealed_tree_manifest_digest(self.files)
        base = launch_environment_digest(dict(self.base_environment))
        root = Path(self.artifact_root)
        cwd = root if self.cwd_relative_path == "." else root / self.cwd_relative_path
        command = launch_command_digest(
            (str(root / self.executable_relative_path), *self.argv_tail), cwd
        )
        role_digests = {
            role: _role_manifest_digest(self.files, role) for role in ARTIFACT_ROLES
        }
        launch_contract = canonical_digest(
            {
                "argv_tail": self.argv_tail,
                "base_environment_digest": base,
                "cwd_relative_path": self.cwd_relative_path,
                "executable_relative_path": self.executable_relative_path,
                "sealed_tree_manifest_digest": tree,
                "schema": "pok-materialized-launch-contract-v1",
            }
        )
        expected = {
            "sealed_tree_manifest_digest": tree,
            "launch_contract_digest": launch_contract,
            "launch_command_digest": command,
            "base_environment_digest": base,
            "model_digest": role_digests["model"],
            "config_digest": role_digests["config"],
            "action_set_digest": role_digests["action_set"],
            "dependency_digest": role_digests["dependency"],
            "runtime_digest": role_digests["runtime"],
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"artifact expectation has a stale {name}")
        if self.identity_digest != _artifact_identity_digest(
            sealed_tree_manifest_digest=tree,
            launch_contract_digest=launch_contract,
            launch_command_digest_value=command,
            base_environment_digest=base,
            model_digest=role_digests["model"],
            config_digest=role_digests["config"],
            action_set_digest=role_digests["action_set"],
            dependency_digest=role_digests["dependency"],
            runtime_digest=role_digests["runtime"],
        ):
            raise ValueError("artifact expectation has a stale identity digest")

    def digest(self) -> str:
        self._verify_digests()
        return canonical_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class VerifiedArtifactMaterialization:
    expectation_digest: str
    identity_digest: str
    artifact_root: str
    launch_contract_digest: str
    launch_command_digest: str
    base_environment_digest: str
    root_device: int
    root_inode: int
    executable_device: int
    executable_inode: int
    executable_is_elf: bool
    verified_epoch_ms: int
    exact_regular_file_manifest: bool
    no_symlinks: bool
    readonly_cas_verified: bool
    _verification_guard: object = None

    def digest(self) -> str:
        payload = asdict(self)
        payload.pop("_verification_guard")
        return canonical_digest(payload)

    def _assert_verified(self) -> None:
        guard = self._verification_guard
        if not callable(guard) or guard(self) is not True:
            raise ValueError("artifact materialization was copied, forged, or altered")


def _enumerate_regular_tree(root: Path) -> tuple[ArtifactFileRecord, ...]:
    records: list[ArtifactFileRecord] = []
    # Roles are supplied by the frozen expectation.  Enumeration uses "other"
    # temporarily and is compared by path/metadata/content below.
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in tuple(dirnames):
            child = directory_path / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ResourceEnforcementError("artifact tree contains a non-directory link/node")
        for name in filenames:
            path = directory_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ResourceEnforcementError("artifact tree contains a non-regular file")
            relative = path.relative_to(root).as_posix()
            descriptor = _open_regular_beneath(root, relative)
            try:
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = os.read(descriptor, 1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    digest.update(chunk)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if (
                size != metadata.st_size
                or after.st_dev != metadata.st_dev
                or after.st_ino != metadata.st_ino
            ):
                raise ResourceEnforcementError("artifact file changed during verification")
            records.append(
                ArtifactFileRecord(
                    relative_path=relative,
                    size_bytes=size,
                    mode=stat.S_IMODE(after.st_mode),
                    sha256=digest.hexdigest(),
                    role="other",
                )
            )
    return tuple(sorted(records, key=lambda item: item.relative_path))


def _open_regular_beneath(root: Path, relative_path: str) -> int:
    parts = Path(_relative_manifest_path(relative_path, "artifact path")).parts
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        for component in parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            os.close(parent)
            parent = child
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise ResourceEnforcementError("artifact manifest entry is not regular")
        return descriptor
    finally:
        os.close(parent)


def freeze_artifact_expectation(
    *,
    artifact_root: str | os.PathLike[str],
    executable_relative_path: str,
    cwd_relative_path: str,
    argv_tail: Sequence[str],
    base_environment: Mapping[str, str],
    role_by_relative_path: Mapping[str, str],
    formal_readonly_cas_required: bool,
) -> ArtifactMaterializationExpectation:
    """Freeze an exact regular-file tree before candidate selection/evaluation."""

    root = Path(artifact_root).resolve(strict=True)
    enumerated = _enumerate_regular_tree(root)
    if set(role_by_relative_path) != {record.relative_path for record in enumerated}:
        raise ValueError("every artifact file must have exactly one frozen content role")
    records = tuple(
        replace(record, role=role_by_relative_path[record.relative_path])
        for record in enumerated
    )
    environment = _normalize_environment(base_environment)
    tree = _sealed_tree_manifest_digest(records)
    base_digest = launch_environment_digest(dict(environment))
    cwd = root if cwd_relative_path == "." else root / cwd_relative_path
    command_digest = launch_command_digest(
        (str(root / executable_relative_path), *tuple(argv_tail)), cwd
    )
    launch_contract = canonical_digest(
        {
            "argv_tail": tuple(argv_tail),
            "base_environment_digest": base_digest,
            "cwd_relative_path": cwd_relative_path,
            "executable_relative_path": executable_relative_path,
            "sealed_tree_manifest_digest": tree,
            "schema": "pok-materialized-launch-contract-v1",
        }
    )
    roles = {role: _role_manifest_digest(records, role) for role in ARTIFACT_ROLES}
    identity = _artifact_identity_digest(
        sealed_tree_manifest_digest=tree,
        launch_contract_digest=launch_contract,
        launch_command_digest_value=command_digest,
        base_environment_digest=base_digest,
        model_digest=roles["model"],
        config_digest=roles["config"],
        action_set_digest=roles["action_set"],
        dependency_digest=roles["dependency"],
        runtime_digest=roles["runtime"],
    )
    return ArtifactMaterializationExpectation(
        artifact_root=str(root),
        executable_relative_path=executable_relative_path,
        cwd_relative_path=cwd_relative_path,
        argv_tail=tuple(argv_tail),
        base_environment=environment,
        files=records,
        sealed_tree_manifest_digest=tree,
        launch_contract_digest=launch_contract,
        launch_command_digest=command_digest,
        base_environment_digest=base_digest,
        model_digest=roles["model"],
        config_digest=roles["config"],
        action_set_digest=roles["action_set"],
        dependency_digest=roles["dependency"],
        runtime_digest=roles["runtime"],
        identity_digest=identity,
        formal_readonly_cas_required=formal_readonly_cas_required,
    )


def verify_artifact_materialization(
    expectation: ArtifactMaterializationExpectation,
) -> VerifiedArtifactMaterialization:
    expectation._verify_digests()
    root = Path(expectation.artifact_root)
    before = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or root.is_symlink():
        raise ResourceEnforcementError("artifact root is not a real directory")
    actual_untyped = _enumerate_regular_tree(root)
    actual_by_path = {record.relative_path: record for record in actual_untyped}
    expected_by_path = {record.relative_path: record for record in expectation.files}
    if set(actual_by_path) != set(expected_by_path):
        raise ResourceEnforcementError("artifact regular-file set differs from the freeze")
    for path, expected in expected_by_path.items():
        actual = actual_by_path[path]
        if actual.content_payload() != expected.content_payload():
            raise ResourceEnforcementError(f"artifact bytes/metadata drifted: {path}")
    after = root.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ResourceEnforcementError("artifact root changed during verification")
    executable_fd = _open_regular_beneath(root, expectation.executable_relative_path)
    try:
        executable = os.fstat(executable_fd)
        executable_prefix = os.read(executable_fd, 4)
    finally:
        os.close(executable_fd)
    if expectation.formal_readonly_cas_required:
        raise FormalEnforcementUnavailable(
            "in-process verification cannot attest a formal read-only CAS mount; "
            "the fixed privileged supervisor must sign the materialization and "
            "actual executable hash"
        )
    receipt = VerifiedArtifactMaterialization(
        expectation_digest=expectation.digest(),
        identity_digest=expectation.identity_digest,
        artifact_root=str(root),
        launch_contract_digest=expectation.launch_contract_digest,
        launch_command_digest=expectation.launch_command_digest,
        base_environment_digest=expectation.base_environment_digest,
        root_device=before.st_dev,
        root_inode=before.st_ino,
        executable_device=executable.st_dev,
        executable_inode=executable.st_ino,
        executable_is_elf=executable_prefix == b"\x7fELF",
        verified_epoch_ms=time.time_ns() // 1_000_000,
        exact_regular_file_manifest=True,
        no_symlinks=True,
        # Owner/mode checks are useful diagnostic evidence, but they are not a
        # mount-level read-only CAS proof.  Only the external supervisor may
        # attest that stronger property.
        readonly_cas_verified=False,
    )
    sealed = receipt.digest()

    def issued(candidate: object, owner: object = receipt, digest: str = sealed) -> bool:
        return (
            candidate is owner
            and isinstance(candidate, VerifiedArtifactMaterialization)
            and candidate.digest() == digest
        )

    object.__setattr__(receipt, "_verification_guard", issued)
    return receipt


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    connection_index: int
    identity_digest: str
    argv: tuple[str, ...]
    cwd: str
    base_environment: tuple[tuple[str, str], ...]
    launch_contract_digest: str
    launch_command_digest: str
    base_environment_digest: str
    thread_environment_digest: str
    launch_environment_digest: str
    artifact_materialization_digest: str
    artifact_expectation: ArtifactMaterializationExpectation
    artifact_root_device: int
    artifact_root_inode: int
    executable_device: int
    executable_inode: int
    actual_policy_seed: int
    run_id: str
    issuer_digest: str
    verifier_digest: str

    @classmethod
    def create(
        cls,
        *,
        profile: EnforcementProfile,
        connection_index: int,
        identity_digest: str,
        argv: Sequence[str],
        cwd: str | os.PathLike[str],
        base_environment: Mapping[str, str],
        artifact_expectation: ArtifactMaterializationExpectation,
        artifact_materialization: VerifiedArtifactMaterialization,
        launch_contract_digest: str,
        actual_policy_seed: int,
        run_id: str,
        issuer_digest: str,
        verifier_digest: str,
    ) -> "ExecutionBinding":
        _strict_int(connection_index, "connection index")
        if connection_index > 1:
            raise ValueError("connection index must be 0 or 1")
        normalized_argv = _normalize_argv(argv)
        normalized_cwd = str(Path(cwd).resolve())
        normalized_base = _normalize_environment(base_environment)
        if not isinstance(
            artifact_materialization, VerifiedArtifactMaterialization
        ):
            raise ValueError("execution requires a typed artifact materialization")
        artifact_materialization._assert_verified()
        if (
            not isinstance(artifact_expectation, ArtifactMaterializationExpectation)
            or artifact_expectation.digest()
            != artifact_materialization.expectation_digest
        ):
            raise ValueError("artifact expectation differs from its verified materialization")
        if artifact_materialization.identity_digest != _digest(
            identity_digest, "identity digest"
        ):
            raise ValueError("execution identity differs from materialized artifact bytes")
        if artifact_materialization.launch_contract_digest != _digest(
            launch_contract_digest, "launch contract digest"
        ):
            raise ValueError("launch contract differs from materialized artifact")
        if artifact_materialization.launch_command_digest != launch_command_digest(
            normalized_argv, normalized_cwd
        ):
            raise ValueError("launch command differs from materialized executable")
        if artifact_materialization.base_environment_digest != launch_environment_digest(
            dict(normalized_base)
        ):
            raise ValueError("base environment differs from materialized launch contract")
        effective = _effective_environment(
            profile, connection_index, actual_policy_seed, dict(normalized_base)
        )
        return cls(
            connection_index=connection_index,
            identity_digest=_digest(identity_digest, "identity digest"),
            argv=normalized_argv,
            cwd=normalized_cwd,
            base_environment=normalized_base,
            launch_contract_digest=_digest(
                launch_contract_digest, "launch contract digest"
            ),
            launch_command_digest=launch_command_digest(normalized_argv, normalized_cwd),
            base_environment_digest=launch_environment_digest(dict(normalized_base)),
            thread_environment_digest=profile.thread_environment_digest,
            launch_environment_digest=launch_environment_digest(effective),
            artifact_materialization_digest=artifact_materialization.digest(),
            artifact_expectation=artifact_expectation,
            artifact_root_device=artifact_materialization.root_device,
            artifact_root_inode=artifact_materialization.root_inode,
            executable_device=artifact_materialization.executable_device,
            executable_inode=artifact_materialization.executable_inode,
            actual_policy_seed=_strict_int(actual_policy_seed, "policy seed"),
            run_id=_digest(run_id, "run ID"),
            issuer_digest=_digest(issuer_digest, "issuer digest"),
            verifier_digest=_digest(verifier_digest, "execution verifier digest"),
        )


@dataclass(frozen=True, slots=True)
class LegLaunchSpec:
    leg_plan_digest: str
    leg_run_id: str
    bindings: tuple[ExecutionBinding, ExecutionBinding]

    def __post_init__(self) -> None:
        object.__setattr__(self, "leg_plan_digest", _digest(self.leg_plan_digest, "leg plan"))
        object.__setattr__(self, "leg_run_id", _digest(self.leg_run_id, "leg run ID"))
        bindings = tuple(self.bindings)
        if len(bindings) != 2 or tuple(item.connection_index for item in bindings) != (0, 1):
            raise ValueError("a formal leg requires connection bindings 0 and 1")
        if bindings[0].identity_digest == bindings[1].identity_digest:
            raise ValueError("a formal leg requires two distinct identities")
        object.__setattr__(self, "bindings", bindings)


def _effective_environment(
    profile: EnforcementProfile,
    connection_index: int,
    policy_seed: int,
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    result = dict(_normalize_environment(base_environment))
    for key, value in profile.thread_environment:
        if key in result and result[key] != value:
            raise ValueError(f"base environment overrides frozen thread variable {key}")
        result[key] = value
    devices = profile.gpu_devices_by_connection[connection_index]
    visible = ",".join(devices)
    for key, value in {
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "CUDA_VISIBLE_DEVICES": visible,
        "NVIDIA_VISIBLE_DEVICES": visible if visible else "none",
        "POK_FORMAL_RESOURCE_ENFORCED": "0",
        "POK_RESOURCE_AUTHORITY": "development_diagnostic_only",
        "POK_POLICY_SEED": str(policy_seed),
    }.items():
        if key in result and result[key] != value:
            raise ValueError(f"base environment overrides enforced variable {key}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class CgroupProbe:
    schema: str
    delegated_root: str
    mount_point: str | None
    mount_id: int | None
    cgroup_v2: bool
    required_controllers: tuple[str, ...]
    available_controllers: tuple[str, ...]
    cpuset_cpus_effective: tuple[int, ...]
    cpuset_mems_effective: str | None
    writable_files: tuple[str, ...]
    empty_internal_process_set: bool
    diagnostic_cgroup_ready: bool
    formal_available: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cgroup2_mounts(mountinfo_path: Path = Path("/proc/self/mountinfo")) -> list[tuple[int, Path]]:
    mounts: list[tuple[int, Path]] = []
    try:
        lines = mountinfo_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return mounts
    for line in lines:
        left, separator, right = line.partition(" - ")
        if not separator or not right.startswith("cgroup2 "):
            continue
        fields = left.split()
        if len(fields) < 5:
            continue
        try:
            mount_id = int(fields[0])
        except ValueError:
            continue
        mount_point = Path(fields[4].replace("\\040", " ")).resolve()
        mounts.append((mount_id, mount_point))
    return mounts


def _require_installed_digest(path: Path, expected: str, kind: str) -> None:
    expected_kind = "regular" if kind == "executable" else kind
    ok, reason = _root_owned_chain_not_mutable(path, expected_kind=expected_kind)
    if not ok:
        raise FormalEnforcementUnavailable(reason)
    if kind in {"regular", "executable"}:
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise FormalEnforcementUnavailable(f"cannot hash {path}") from exc
        if actual != expected:
            raise FormalEnforcementUnavailable(f"installed digest mismatch for {path}")
        if kind == "executable":
            try:
                metadata = path.stat(follow_symlinks=False)
                with path.open("rb") as executable:
                    magic = executable.read(4)
            except OSError as exc:
                raise FormalEnforcementUnavailable(
                    f"cannot inspect installed executable {path}"
                ) from exc
            if not metadata.st_mode & 0o111 or magic != b"\x7fELF":
                raise FormalEnforcementUnavailable(
                    f"installed verifier/supervisor is not an executable ELF: {path}"
                )


def decode_supervisor_attestation(payload: bytes) -> SupervisorAttestation:
    """Strictly decode a readiness attestation without granting authority.

    The accepted key set is derived from the dataclass itself so schema changes
    cannot silently leave the wire decoder one field behind the signed object.
    """

    if len(payload) > MAX_TEXT_BYTES:
        raise FormalEnforcementUnavailable("supervisor attestation is oversized")
    try:
        decoded = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FormalEnforcementUnavailable("supervisor attestation is malformed") from exc
    if not isinstance(decoded, dict):
        raise FormalEnforcementUnavailable("supervisor attestation is not an object")
    expected = set(SupervisorAttestation.__dataclass_fields__)
    if set(decoded) != expected:
        raise FormalEnforcementUnavailable(
            "supervisor attestation has unknown or missing fields"
        )
    if (
        not isinstance(decoded["bot_uids_by_connection"], list)
        or len(decoded["bot_uids_by_connection"]) != 2
    ):
        raise FormalEnforcementUnavailable(
            "supervisor attestation has an invalid ordered UID pair"
        )
    try:
        return SupervisorAttestation(
            **{
                **decoded,
                "bot_uids_by_connection": tuple(decoded["bot_uids_by_connection"]),
            }
        )
    except (TypeError, ValueError) as exc:
        raise FormalEnforcementUnavailable("supervisor attestation fields are invalid") from exc


# Backward-compatible private spelling for callers predating the public strict
# decoder.  Both names execute the same field-derived decoder.
_decode_supervisor_attestation = decode_supervisor_attestation


def _decode_signed_supervisor_object(
    payload: bytes,
    *,
    expected_fields: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(payload, bytes) or len(payload) > MAX_TEXT_BYTES:
        raise FormalEnforcementUnavailable(f"{label} is oversized or not bytes")
    try:
        decoded = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FormalEnforcementUnavailable(f"{label} is malformed") from exc
    if not isinstance(decoded, dict) or set(decoded) != expected_fields:
        raise FormalEnforcementUnavailable(
            f"{label} has unknown or missing fields"
        )
    return decoded


def decode_supervisor_launch_authorization(
    payload: bytes,
) -> SupervisorLaunchAuthorization:
    """Strictly decode, but do not locally bless, a signed prelaunch reply."""

    decoded = _decode_signed_supervisor_object(
        payload,
        expected_fields=set(SupervisorLaunchAuthorization.__dataclass_fields__),
        label="supervisor launch authorization",
    )
    pair_fields = (
        "ordered_identity_digests",
        "ordered_materialization_digests",
        "ordered_launch_command_digests",
        "ordered_base_environment_digests",
        "ordered_launch_environment_digests",
        "ordered_issuer_digests",
        "ordered_execution_verifier_digests",
        "ordered_policy_seeds",
        "ordered_process_uids",
    )
    if any(
        not isinstance(decoded[name], list) or len(decoded[name]) != 2
        for name in pair_fields
    ):
        raise FormalEnforcementUnavailable(
            "supervisor launch authorization has invalid ordered pairs"
        )
    try:
        return SupervisorLaunchAuthorization(
            **{
                **decoded,
                **{name: tuple(decoded[name]) for name in pair_fields},
            }
        )
    except (TypeError, ValueError) as exc:
        raise FormalEnforcementUnavailable(
            "supervisor launch authorization fields are invalid"
        ) from exc


def decode_supervisor_leg_receipt(payload: bytes) -> SupervisorLegReceipt:
    """Strictly decode the signed post-run capture/resource receipt."""

    decoded = _decode_signed_supervisor_object(
        payload,
        expected_fields=set(SupervisorLegReceipt.__dataclass_fields__),
        label="supervisor leg receipt",
    )
    pair_fields = (
        "ordered_identity_digests",
        "ordered_materialization_digests",
        "ordered_launch_command_digests",
        "ordered_base_environment_digests",
        "ordered_launch_environment_digests",
        "ordered_policy_seeds",
        "ordered_process_pids",
        "ordered_process_group_ids",
        "ordered_process_start_ticks",
        "ordered_process_uids",
        "ordered_cgroup_paths",
        "ordered_cgroup_inodes",
        "execution_raw_record_digests",
        "resource_raw_record_digests",
        "ordered_issuer_digests",
        "ordered_execution_verifier_digests",
        "termination_kinds",
    )
    if any(
        not isinstance(decoded[name], list) or len(decoded[name]) != 2
        for name in pair_fields
    ):
        raise FormalEnforcementUnavailable(
            "supervisor leg receipt has invalid ordered pairs"
        )
    sockets = decoded.get("ordered_socket_identities")
    events = decoded.get("decision_events")
    if (
        not isinstance(sockets, list)
        or len(sockets) != 2
        or any(
            not isinstance(item, dict)
            or set(item) != set(SocketCaptureIdentity.__dataclass_fields__)
            for item in sockets
        )
        or not isinstance(events, list)
        or any(
            not isinstance(item, dict)
            or set(item) != set(DecisionEnforcementEvent.__dataclass_fields__)
            for item in events
        )
    ):
        raise FormalEnforcementUnavailable(
            "supervisor leg receipt has malformed socket/decision capture rows"
        )
    try:
        return SupervisorLegReceipt(
            **{
                **decoded,
                **{name: tuple(decoded[name]) for name in pair_fields},
                "ordered_socket_identities": tuple(
                    SocketCaptureIdentity(**item) for item in sockets
                ),
                "decision_events": tuple(
                    DecisionEnforcementEvent(**item) for item in events
                ),
            }
        )
    except (TypeError, ValueError) as exc:
        raise FormalEnforcementUnavailable(
            "supervisor leg receipt fields are invalid"
        ) from exc


def decode_supervisor_consumption_ledger_entry(
    payload: bytes,
) -> SupervisorConsumptionLedgerEntry:
    decoded = _decode_signed_supervisor_object(
        payload,
        expected_fields=set(SupervisorConsumptionLedgerEntry.__dataclass_fields__),
        label="supervisor consumption ledger entry",
    )
    for name in ("ordered_socket_identity_digests", "termination_kinds"):
        if not isinstance(decoded[name], list) or len(decoded[name]) != 2:
            raise FormalEnforcementUnavailable(
                "supervisor consumption ledger entry has invalid ordered pairs"
            )
    try:
        return SupervisorConsumptionLedgerEntry(
            **{
                **decoded,
                "ordered_socket_identity_digests": tuple(
                    decoded["ordered_socket_identity_digests"]
                ),
                "termination_kinds": tuple(decoded["termination_kinds"]),
            }
        )
    except (TypeError, ValueError) as exc:
        raise FormalEnforcementUnavailable(
            "supervisor consumption ledger entry fields are invalid"
        ) from exc


def decode_supervisor_attempt_journal_entry(
    payload: bytes,
) -> SupervisorAttemptJournalEntry:
    decoded = _decode_signed_supervisor_object(
        payload,
        expected_fields=set(SupervisorAttemptJournalEntry.__dataclass_fields__),
        label="supervisor attempt journal entry",
    )
    try:
        return SupervisorAttemptJournalEntry(**decoded)
    except (TypeError, ValueError) as exc:
        raise FormalEnforcementUnavailable(
            "supervisor attempt journal entry fields are invalid"
        ) from exc


def decode_supervisor_attempt_journal_seal(
    payload: bytes,
) -> SupervisorAttemptJournalSeal:
    decoded = _decode_signed_supervisor_object(
        payload,
        expected_fields=set(SupervisorAttemptJournalSeal.__dataclass_fields__),
        label="supervisor attempt journal seal",
    )
    try:
        return SupervisorAttemptJournalSeal(**decoded)
    except (TypeError, ValueError) as exc:
        raise FormalEnforcementUnavailable(
            "supervisor attempt journal seal fields are invalid"
        ) from exc


def _verify_external_signature(
    contract: TrustedSupervisorContract, attestation: SupervisorAttestation
) -> None:
    verifier = Path(contract.verifier_executable)
    public_key = Path(contract.public_key_path)
    _require_installed_digest(
        verifier, contract.verifier_executable_sha256, "executable"
    )
    _require_installed_digest(public_key, contract.public_key_sha256, "regular")
    envelope = _canonical_bytes(
        {
            "payload": attestation.unsigned_payload(),
            "payload_digest": attestation.payload_digest(),
            "schema": "pok-supervisor-signature-envelope-v1",
            "signature_hex": attestation.signature_hex,
        }
    )
    try:
        completed = subprocess.run(
            (
                str(verifier),
                "--verify-pok-supervisor-attestation",
                str(public_key),
            ),
            input=envelope,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
            close_fds=True,
            env={"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FormalEnforcementUnavailable(
            "external supervisor signature verifier failed"
        ) from exc
    expected = f"VALID {attestation.payload_digest()}\n".encode("ascii")
    if completed.returncode != 0 or completed.stdout != expected:
        raise FormalEnforcementUnavailable(
            "external supervisor signature verification was rejected"
        )


def _verify_external_launch_signature(
    contract: TrustedSupervisorContract,
    authorization: SupervisorLaunchAuthorization,
) -> None:
    verifier = Path(contract.verifier_executable)
    public_key = Path(contract.public_key_path)
    _require_installed_digest(
        verifier, contract.verifier_executable_sha256, "executable"
    )
    _require_installed_digest(public_key, contract.public_key_sha256, "regular")
    envelope = _canonical_bytes(
        {
            "payload": authorization.unsigned_payload(),
            "payload_digest": authorization.payload_digest(),
            "schema": "pok-supervisor-launch-signature-envelope-v1",
            "signature_hex": authorization.signature_hex,
        }
    )
    try:
        completed = subprocess.run(
            (
                str(verifier),
                "--verify-pok-supervisor-launch-authorization",
                str(public_key),
            ),
            input=envelope,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
            close_fds=True,
            env={"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FormalEnforcementUnavailable(
            "external supervisor launch signature verifier failed"
        ) from exc
    expected = f"VALID {authorization.payload_digest()}\n".encode("ascii")
    if completed.returncode != 0 or completed.stdout != expected:
        raise FormalEnforcementUnavailable(
            "external supervisor launch signature verification was rejected"
        )


def _verify_external_leg_signature(
    contract: TrustedSupervisorContract, receipt: SupervisorLegReceipt
) -> None:
    verifier = Path(contract.verifier_executable)
    public_key = Path(contract.public_key_path)
    _require_installed_digest(
        verifier, contract.verifier_executable_sha256, "executable"
    )
    _require_installed_digest(public_key, contract.public_key_sha256, "regular")
    envelope = _canonical_bytes(
        {
            "payload": receipt.unsigned_payload(),
            "payload_digest": receipt.payload_digest(),
            "schema": "pok-supervisor-leg-signature-envelope-v1",
            "signature_hex": receipt.signature_hex,
        }
    )
    try:
        completed = subprocess.run(
            (
                str(verifier),
                "--verify-pok-supervisor-leg-receipt",
                str(public_key),
            ),
            input=envelope,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
            close_fds=True,
            env={"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FormalEnforcementUnavailable(
            "external supervisor leg signature verifier failed"
        ) from exc
    expected = f"VALID {receipt.payload_digest()}\n".encode("ascii")
    if completed.returncode != 0 or completed.stdout != expected:
        raise FormalEnforcementUnavailable(
            "external supervisor leg signature verification was rejected"
        )


def _verify_external_journal_signature(
    contract: TrustedSupervisorContract,
    signed: SupervisorAttemptJournalEntry | SupervisorAttemptJournalSeal,
) -> None:
    verifier = Path(contract.verifier_executable)
    public_key = Path(contract.public_key_path)
    _require_installed_digest(
        verifier, contract.verifier_executable_sha256, "executable"
    )
    _require_installed_digest(public_key, contract.public_key_sha256, "regular")
    if isinstance(signed, SupervisorAttemptJournalEntry):
        flag = "--verify-pok-supervisor-attempt-journal-entry"
        envelope_schema = "pok-supervisor-attempt-journal-entry-signature-envelope-v1"
        label = "attempt journal entry"
    elif isinstance(signed, SupervisorAttemptJournalSeal):
        flag = "--verify-pok-supervisor-attempt-journal-seal"
        envelope_schema = "pok-supervisor-attempt-journal-seal-signature-envelope-v1"
        label = "attempt journal seal"
    else:  # pragma: no cover - protected by the public type contract
        raise TypeError("unsupported supervisor journal signature object")
    envelope = _canonical_bytes(
        {
            "payload": signed.unsigned_payload(),
            "payload_digest": signed.payload_digest(),
            "schema": envelope_schema,
            "signature_hex": signed.signature_hex,
        }
    )
    try:
        completed = subprocess.run(
            (str(verifier), flag, str(public_key)),
            input=envelope,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
            close_fds=True,
            env={"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FormalEnforcementUnavailable(
            f"external supervisor {label} signature verifier failed"
        ) from exc
    expected = f"VALID {signed.payload_digest()}\n".encode("ascii")
    if completed.returncode != 0 or completed.stdout != expected:
        raise FormalEnforcementUnavailable(
            f"external supervisor {label} signature verification was rejected"
        )


def probe_trusted_supervisor(
    *,
    contract_path: Path = TRUSTED_SUPERVISOR_CONTRACT_PATH,
    control_fd: int | None = None,
    connect_timeout_sec: float = 1.0,
) -> TrustedSupervisorProbe:
    """Strict online read-only probe for the external formal authority.

    A raw writable cgroup delegation is deliberately insufficient.  Formal
    availability requires a fixed root-owned contract, pinned executable/key,
    root-owned ``SOCK_SEQPACKET`` service, uid-0 peer credentials, a fresh
    nonce-bound signed response, and readback of the supervisor-owned cgroup,
    CAS and global-lock identities.  No current in-process launcher can make
    this probe pass.
    """

    reasons: list[str] = []
    contract: TrustedSupervisorContract | None = None
    attestation: SupervisorAttestation | None = None
    control_session_digest: str | None = None
    if contract_path != TRUSTED_SUPERVISOR_CONTRACT_PATH:
        reasons.append("formal supervisor contract path is fixed")
    else:
        try:
            contract = TrustedSupervisorContract.from_fixed_file(contract_path)
        except FormalEnforcementUnavailable as exc:
            reasons.append(str(exc))
    if contract is not None:
        installations = (
            (
                Path(contract.supervisor_executable),
                contract.supervisor_executable_sha256,
                "executable",
            ),
            (
                Path(contract.verifier_executable),
                contract.verifier_executable_sha256,
                "executable",
            ),
            (Path(contract.public_key_path), contract.public_key_sha256, "regular"),
            (Path(contract.control_socket_path), "", "socket"),
            (Path(contract.control_cgroup_root), "", "directory"),
            (Path(contract.artifact_cas_root), "", "directory"),
            (Path(contract.consumption_ledger_root), "", "directory"),
            (Path(contract.attempt_journal_root), "", "directory"),
            (FORMAL_GLOBAL_LOCK_PATH, "", "regular"),
        )
        for path, expected_digest, kind in installations:
            try:
                _require_installed_digest(path, expected_digest, kind)
            except FormalEnforcementUnavailable as exc:
                reasons.append(str(exc))
        if os.geteuid() != contract.evaluator_uid:
            reasons.append("probe evaluator uid differs from the fixed contract")
        if not reasons and (type(control_fd) is not int or control_fd < 0):
            reasons.append(
                "trusted launcher did not supply the required pre-opened supervisor control fd"
            )
        if not reasons:
            nonce = secrets.token_hex(32)
            client = socket.socket(fileno=os.dup(control_fd))
            client.settimeout(connect_timeout_sec)
            try:
                if (
                    client.family != socket.AF_UNIX
                    or client.type & 0xF != socket.SOCK_SEQPACKET
                    or client.getpeername() != contract.control_socket_path
                ):
                    raise FormalEnforcementUnavailable(
                        "pre-opened control fd is not the contracted connected SOCK_SEQPACKET channel"
                    )
                peer = client.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                peer_pid, peer_uid, _peer_gid = struct.unpack("3i", peer)
                if peer_uid != contract.service_uid or peer_uid != 0:
                    raise FormalEnforcementUnavailable(
                        "supervisor socket peer is not the contracted uid-0 service"
                    )
                evaluator_cgroup = _read_self_cgroup_path()
                control_session_digest = canonical_digest(
                    {
                        "boot_id": _read_boot_id(),
                        "contract_digest": contract.digest(),
                        "evaluator_cgroup": evaluator_cgroup,
                        "evaluator_uid": os.geteuid(),
                        "local_socket_inode": os.fstat(client.fileno()).st_ino,
                        "nonce": nonce,
                        "peer_pid": peer_pid,
                        "peer_uid": peer_uid,
                        "schema": "pok-supervisor-control-session-v1",
                    }
                )
                request = _canonical_bytes(
                    {
                        "contract_digest": contract.digest(),
                        "control_session_digest": control_session_digest,
                        "evaluator_cgroup": evaluator_cgroup,
                        "nonce": nonce,
                        "schema": "pok-trusted-resource-supervisor-probe-request-v1",
                    }
                )
                client.sendall(request)
                response = client.recv(MAX_TEXT_BYTES + 1)
                attestation = _decode_supervisor_attestation(response)
                attestation.validate(
                    contract, now_epoch_ms=time.time_ns() // 1_000_000
                )
                if attestation.nonce != nonce:
                    raise FormalEnforcementUnavailable(
                        "supervisor response is not bound to this probe nonce"
                    )
                if attestation.control_session_digest != control_session_digest:
                    raise FormalEnforcementUnavailable(
                        "supervisor response belongs to another control session"
                    )
                if attestation.supervisor_pid != peer_pid:
                    raise FormalEnforcementUnavailable(
                        "supervisor attestation pid differs from SO_PEERCRED"
                    )
                _verify_supervisor_process(attestation)
                _verify_supervisor_installation_identity(contract, attestation)
                _verify_external_signature(contract, attestation)
            except (OSError, ValueError, FormalEnforcementUnavailable) as exc:
                reasons.append(str(exc))
                attestation = None
                control_session_digest = None
            finally:
                client.close()
    return TrustedSupervisorProbe(
        schema="pok-trusted-resource-supervisor-probe-v1",
        contract_path=str(TRUSTED_SUPERVISOR_CONTRACT_PATH),
        attestation_path=str(TRUSTED_SUPERVISOR_ATTESTATION_PATH),
        contract_digest=contract.digest() if contract is not None else None,
        attestation_digest=(
            attestation.payload_digest() if attestation is not None else None
        ),
        control_session_digest=(
            control_session_digest if attestation is not None else None
        ),
        formal_available=contract is not None and attestation is not None and not reasons,
        reasons=tuple(reasons),
    )


def _read_self_cgroup_path() -> str:
    try:
        rows = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise FormalEnforcementUnavailable("cannot read evaluator cgroup identity") from exc
    unified = [row.partition("::")[2] for row in rows if "::" in row]
    if len(unified) != 1 or not unified[0].startswith("/"):
        raise FormalEnforcementUnavailable("evaluator lacks one unified cgroup identity")
    return unified[0]


def _verify_supervisor_process(attestation: SupervisorAttestation) -> None:
    stat_path = Path(f"/proc/{attestation.supervisor_pid}/stat")
    cgroup_path = Path(f"/proc/{attestation.supervisor_pid}/cgroup")
    try:
        fields = stat_path.read_text(encoding="ascii").split()
        start_ticks = int(fields[21])
        cgroup_rows = cgroup_path.read_text(encoding="ascii").splitlines()
    except (OSError, ValueError, IndexError) as exc:
        raise FormalEnforcementUnavailable(
            "cannot bind supervisor pid/start/cgroup"
        ) from exc
    unified = [row.partition("::")[2] for row in cgroup_rows if "::" in row]
    if (
        start_ticks != attestation.supervisor_start_ticks
        or unified != [attestation.supervisor_cgroup]
    ):
        raise FormalEnforcementUnavailable("supervisor pid identity changed")


def _verify_supervisor_installation_identity(
    contract: TrustedSupervisorContract, attestation: SupervisorAttestation
) -> None:
    try:
        control = Path(contract.control_cgroup_root).stat(follow_symlinks=False)
        cas = Path(contract.artifact_cas_root).stat(follow_symlinks=False)
        ledger = Path(contract.consumption_ledger_root).stat(follow_symlinks=False)
        journal = Path(contract.attempt_journal_root).stat(follow_symlinks=False)
        lock = FORMAL_GLOBAL_LOCK_PATH.stat(follow_symlinks=False)
    except OSError as exc:
        raise FormalEnforcementUnavailable(
            "supervisor installation identity disappeared"
        ) from exc
    if (
        control.st_ino != attestation.control_cgroup_inode
        or cas.st_ino != attestation.artifact_cas_inode
        or ledger.st_ino != attestation.consumption_ledger_root_inode
        or journal.st_ino != attestation.attempt_journal_root_inode
        or lock.st_ino != attestation.global_lock_inode
    ):
        raise FormalEnforcementUnavailable(
            "supervisor cgroup/CAS/global-lock identity differs from attestation"
        )
    mount_matches = [
        mount_id
        for mount_id, mount in _cgroup2_mounts()
        if Path(contract.control_cgroup_root) == mount
        or mount in Path(contract.control_cgroup_root).parents
    ]
    if not mount_matches or mount_matches[-1] != attestation.control_cgroup_mount_id:
        raise FormalEnforcementUnavailable(
            "supervisor cgroup mount differs from attestation"
        )


def _read_root_owned_no_clobber_file(
    root: Path,
    filename: str,
    *,
    expected_inode: int | None = None,
) -> tuple[bytes, int]:
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\x00" in filename
    ):
        raise FormalEnforcementUnavailable("trusted ledger filename is invalid")
    ok, reason = _root_owned_chain_not_mutable(root, expected_kind="directory")
    if not ok:
        raise FormalEnforcementUnavailable(reason)
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise FormalEnforcementUnavailable(
            "trusted ledger root is unavailable"
        ) from exc
    descriptor = -1
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o222
            or metadata.st_nlink != 1
        ):
            raise FormalEnforcementUnavailable(
                "trusted ledger entry is not a root-owned read-only no-hardlink file"
            )
        if expected_inode is not None and metadata.st_ino != expected_inode:
            raise FormalEnforcementUnavailable(
                "trusted ledger entry inode differs from the signed receipt"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, MAX_TEXT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_TEXT_BYTES:
                raise FormalEnforcementUnavailable("trusted ledger entry is oversized")
        return b"".join(chunks), metadata.st_ino
    except OSError as exc:
        raise FormalEnforcementUnavailable(
            "trusted ledger entry is unavailable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(root_fd)


def _verify_durable_consumption_ledger_entry(
    contract: TrustedSupervisorContract,
    readiness: SupervisorAttestation,
    launch: SupervisorLaunchAuthorization,
    receipt: SupervisorLegReceipt,
) -> tuple[SupervisorConsumptionLedgerEntry, str]:
    filename = f"{receipt.receipt_consumption_key}.json"
    payload, _inode = _read_root_owned_no_clobber_file(
        Path(contract.consumption_ledger_root),
        filename,
        expected_inode=receipt.consumption_ledger_entry_inode,
    )
    entry = decode_supervisor_consumption_ledger_entry(payload)
    entry.validate(contract, readiness, launch, receipt)
    canonical = _canonical_bytes(asdict(entry))
    if payload != canonical or entry.digest() != receipt.consumption_ledger_entry_digest:
        raise FormalEnforcementUnavailable(
            "durable consumption ledger bytes differ from the signed receipt"
        )
    return entry, str(Path(contract.consumption_ledger_root) / filename)


def _verify_signed_supervisor_attempt_journal_material(
    *,
    readiness_by_digest: Mapping[str, SupervisorAttestation],
    launch_authorizations: Sequence[SupervisorLaunchAuthorization],
    entries: Sequence[SupervisorAttemptJournalEntry],
    seal: SupervisorAttemptJournalSeal,
    expected_scope_digest: str,
) -> TrustedSupervisorContract:
    """Verify a closed, durable attempt scope including aborted launches.

    This is the formal anti-selection boundary.  Every signed launch in the
    scope must have one contiguous terminal row, and the externally signed
    closed head must cover the exact ordered row list.  The fixed root-owned
    journal files are reread, so an in-memory set or caller-selected subset can
    never substitute for durable evidence.
    """

    contract = TrustedSupervisorContract.from_fixed_file()
    launches = tuple(launch_authorizations)
    materialized = tuple(entries)
    if not launches or len(launches) != len(materialized):
        raise FormalEnforcementUnavailable(
            "closed attempt journal requires one terminal row per signed launch"
        )
    scope = _digest(expected_scope_digest, "expected attempt journal scope")
    for launch, entry in zip(launches, materialized):
        readiness = readiness_by_digest.get(launch.readiness_attestation_digest)
        if not isinstance(readiness, SupervisorAttestation):
            raise FormalEnforcementUnavailable(
                "attempt journal launch lacks its signed readiness attestation"
            )
        # Journal rows are restart-durable historical evidence.  The signed
        # boot identity must be canonical, but need not equal the verifier's
        # current boot after a crash/reboot resume.
        readiness.validate(
            contract,
            now_epoch_ms=launch.issued_epoch_ms,
            require_current_boot=False,
        )
        _verify_external_signature(contract, readiness)
        launch.validate(contract, readiness)
        _verify_external_launch_signature(contract, launch)
        entry.validate(contract, launch)
        _verify_external_journal_signature(contract, entry)
        if launch.attempt_journal_scope_digest != scope:
            raise FormalEnforcementUnavailable(
                "attempt journal launch belongs to another closed scope"
            )
        filename = f"{entry.attempt_sequence:020d}-{entry.payload_digest()}.json"
        payload, _inode = _read_root_owned_no_clobber_file(
            Path(contract.attempt_journal_root), filename
        )
        if payload != _canonical_bytes(asdict(entry)):
            raise FormalEnforcementUnavailable(
                "durable attempt journal entry differs from signed bytes"
            )
    seal.validate(contract, materialized, expected_scope_digest=scope)
    _verify_external_journal_signature(contract, seal)
    seal_filename = f"{scope}.seal.json"
    seal_payload, _inode = _read_root_owned_no_clobber_file(
        Path(contract.attempt_journal_root), seal_filename
    )
    if seal_payload != _canonical_bytes(asdict(seal)):
        raise FormalEnforcementUnavailable(
            "durable attempt journal seal differs from signed bytes"
        )
    return contract


@dataclass(frozen=True, slots=True)
class AuthorizedSupervisorAttemptJournal:
    """Content-bound capability for one externally closed attempt scope."""

    scope_digest: str
    contract_digest: str
    seal_digest: str
    entries: tuple[SupervisorAttemptJournalEntry, ...]
    entry_digests: tuple[str, ...]
    readiness_attestation_digests: tuple[str, ...]
    launch_authorization_digests: tuple[str, ...]
    first_attempt_sequence: int
    last_attempt_sequence: int
    entry_count: int
    head_entry_digest: str
    ordered_entry_chain_digest: str
    _readiness_attestations: tuple[SupervisorAttestation, ...] = field(
        init=False, repr=False, compare=False
    )
    _launch_authorizations: tuple[SupervisorLaunchAuthorization, ...] = field(
        init=False, repr=False, compare=False
    )
    _seal: SupervisorAttemptJournalSeal = field(
        init=False, repr=False, compare=False
    )
    _authority_guard: object = field(init=False, repr=False, compare=False)

    def _sealed_payload(self) -> dict[str, Any]:
        return {
            "contract_digest": self.contract_digest,
            "entry_count": self.entry_count,
            "entry_digests": self.entry_digests,
            "first_attempt_sequence": self.first_attempt_sequence,
            "head_entry_digest": self.head_entry_digest,
            "last_attempt_sequence": self.last_attempt_sequence,
            "launch_authorization_digests": self.launch_authorization_digests,
            "ordered_entry_chain_digest": self.ordered_entry_chain_digest,
            "readiness_attestation_digests": self.readiness_attestation_digests,
            "schema": "pok-authorized-supervisor-attempt-journal-v1",
            "scope_digest": self.scope_digest,
            "seal_digest": self.seal_digest,
        }

    def _assert_authorized(self) -> None:
        guard = getattr(self, "_authority_guard", None)
        if not callable(guard) or guard(self) is not True:
            raise FormalEnforcementUnavailable(
                "attempt journal capability was copied, altered, or not externally authorized"
            )
        readiness = {
            item.payload_digest(): item for item in self._readiness_attestations
        }
        contract = _verify_signed_supervisor_attempt_journal_material(
            readiness_by_digest=readiness,
            launch_authorizations=self._launch_authorizations,
            entries=self.entries,
            seal=self._seal,
            expected_scope_digest=self.scope_digest,
        )
        if (
            contract.digest() != self.contract_digest
            or self._seal.payload_digest() != self.seal_digest
            or tuple(item.payload_digest() for item in self.entries)
            != self.entry_digests
        ):
            raise FormalEnforcementUnavailable(
                "attempt journal signed material differs from the sealed capability"
            )

    def projection_payload(self) -> dict[str, Any]:
        self._assert_authorized()
        return {
            **self._sealed_payload(),
            "entries": tuple(asdict(item) for item in self.entries),
        }


def verify_signed_supervisor_attempt_journal(
    *,
    readiness_by_digest: Mapping[str, SupervisorAttestation],
    launch_authorizations: Sequence[SupervisorLaunchAuthorization],
    entries: Sequence[SupervisorAttemptJournalEntry],
    seal: SupervisorAttemptJournalSeal,
    expected_scope_digest: str,
) -> AuthorizedSupervisorAttemptJournal:
    """Authorize a durable closed journal; never return a bare caller-mixable hash."""

    launches = tuple(launch_authorizations)
    materialized = tuple(entries)
    contract = _verify_signed_supervisor_attempt_journal_material(
        readiness_by_digest=readiness_by_digest,
        launch_authorizations=launches,
        entries=materialized,
        seal=seal,
        expected_scope_digest=expected_scope_digest,
    )
    readiness = tuple(
        {
            launch.readiness_attestation_digest: readiness_by_digest[
                launch.readiness_attestation_digest
            ]
            for launch in launches
        }.values()
    )
    capability = AuthorizedSupervisorAttemptJournal(
        scope_digest=_digest(expected_scope_digest, "attempt journal scope"),
        contract_digest=contract.digest(),
        seal_digest=seal.payload_digest(),
        entries=materialized,
        entry_digests=tuple(item.payload_digest() for item in materialized),
        readiness_attestation_digests=tuple(
            item.payload_digest() for item in readiness
        ),
        launch_authorization_digests=tuple(
            item.payload_digest() for item in launches
        ),
        first_attempt_sequence=seal.first_attempt_sequence,
        last_attempt_sequence=seal.last_attempt_sequence,
        entry_count=seal.entry_count,
        head_entry_digest=seal.head_entry_digest,
        ordered_entry_chain_digest=seal.ordered_entry_chain_digest,
    )
    object.__setattr__(capability, "_readiness_attestations", readiness)
    object.__setattr__(capability, "_launch_authorizations", launches)
    object.__setattr__(capability, "_seal", seal)
    sealed = canonical_digest(capability._sealed_payload())

    def issued(
        candidate: object,
        owner: object = capability,
        expected: str = sealed,
    ) -> bool:
        return (
            candidate is owner
            and isinstance(candidate, AuthorizedSupervisorAttemptJournal)
            and canonical_digest(candidate._sealed_payload()) == expected
        )

    object.__setattr__(capability, "_authority_guard", issued)
    return capability


def probe_resource_enforcer(
    delegated_root: str | os.PathLike[str],
    *,
    mountinfo_path: str | os.PathLike[str] = "/proc/self/mountinfo",
) -> CgroupProbe:
    """Read-only capability probe; it never creates or writes a cgroup."""

    root = Path(os.path.abspath(os.fspath(delegated_root)))
    reasons: list[str] = []
    if root.resolve(strict=False) != root:
        reasons.append("delegated root or one of its parents traverses a symlink")
    mount_candidates = [
        (mount_id, mount)
        for mount_id, mount in _cgroup2_mounts(Path(mountinfo_path))
        if root == mount or mount in root.parents
    ]
    mount_id: int | None = None
    mount_point: Path | None = None
    if mount_candidates:
        mount_id, mount_point = max(mount_candidates, key=lambda item: len(item[1].parts))
    else:
        reasons.append("delegated root is not beneath a cgroup2 mount")
    if mount_point is not None and root == mount_point:
        reasons.append("cgroup2 mount root is not a delegated child subtree")
    if not root.exists() or root.is_symlink() or not root.is_dir():
        reasons.append("delegated root is absent, a symlink, or not a directory")

    controllers: tuple[str, ...] = ()
    cpus: tuple[int, ...] = ()
    mems: str | None = None
    writable: list[str] = []
    empty = False
    if root.is_dir():
        try:
            controllers = tuple(sorted((root / "cgroup.controllers").read_text().split()))
        except OSError:
            reasons.append("cgroup.controllers is unreadable")
        missing = sorted(set(REQUIRED_CONTROLLERS) - set(controllers))
        if missing:
            reasons.append(f"required controllers unavailable: {','.join(missing)}")
        try:
            cpus = _parse_cpuset((root / "cpuset.cpus.effective").read_text())
        except (OSError, ValueError):
            reasons.append("cpuset.cpus.effective is unreadable or empty")
        try:
            mems = (root / "cpuset.mems.effective").read_text().strip()
            if not mems:
                raise ValueError
        except (OSError, ValueError):
            reasons.append("cpuset.mems.effective is unreadable or empty")
            mems = None
        try:
            empty = not (root / "cgroup.procs").read_text().strip()
            if not empty:
                reasons.append("delegated root contains internal processes")
        except OSError:
            reasons.append("cgroup.procs is unreadable")
        for name in ("cgroup.procs", "cgroup.subtree_control"):
            path = root / name
            if os.access(path, os.W_OK):
                writable.append(name)
            else:
                reasons.append(f"{name} is not writable")
        if os.access(root, os.W_OK):
            writable.append(".")
        else:
            reasons.append("delegated root cannot create child cgroups")
        if not (root / "cgroup.kill").exists():
            reasons.append("cgroup.kill is unavailable")
        try:
            if (root / "cgroup.type").read_text().strip() != "domain":
                reasons.append("delegated root is not a domain cgroup")
        except OSError:
            reasons.append("cgroup.type is unreadable")
    diagnostic_ready = not reasons
    reasons.append(
        "direct same-uid cgroup delegation is diagnostic only: candidates can "
        "discover and mutate sibling/parent controls; a signed trusted-supervisor "
        "probe is mandatory for formal evaluation"
    )
    return CgroupProbe(
        schema="pok-resource-enforcer-probe-v1",
        delegated_root=str(root),
        mount_point=str(mount_point) if mount_point else None,
        mount_id=mount_id,
        cgroup_v2=mount_point is not None,
        required_controllers=REQUIRED_CONTROLLERS,
        available_controllers=controllers,
        cpuset_cpus_effective=cpus,
        cpuset_mems_effective=mems,
        writable_files=tuple(sorted(writable)),
        empty_internal_process_set=empty,
        diagnostic_cgroup_ready=diagnostic_ready,
        formal_available=False,
        reasons=tuple(reasons),
    )


class CgroupV2Ops:
    """Same-UID diagnostic syscall boundary.

    This backend can exercise cgroup mechanics, but it is permanently
    ineligible for formal evidence because a candidate running as the evaluator
    UID can discover, mutate, or escape writable delegated controls.
    """

    backend_kind = "diagnostic-same-uid-cgroup-v2"
    formal_eligible = False

    def __init__(self, *, mount_point: Path, mount_id: int, reported_root: Path) -> None:
        self.mount_point = mount_point.resolve()
        self.mount_id = mount_id
        self.reported_root = reported_root

    def create_cgroup(self, path: Path) -> None:
        path.mkdir(mode=0o700)

    def read(self, path: Path) -> str:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(65_536, MAX_TEXT_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_TEXT_BYTES:
                    raise ResourceEnforcementError(f"oversized cgroup file: {path}")
                chunks.append(chunk)
            return b"".join(chunks).decode("ascii", "strict")
        finally:
            os.close(descriptor)

    def write(self, path: Path, value: str) -> None:
        if "\x00" in value:
            raise ValueError("cgroup control value contains NUL")
        descriptor = os.open(path, os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            payload = value.encode("ascii")
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise ResourceEnforcementError(f"short write to cgroup file: {path}")
        finally:
            os.close(descriptor)

    def inode(self, path: Path) -> int:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ResourceEnforcementError("cgroup node is not a directory")
        return metadata.st_ino

    def is_writable(self, path: Path) -> bool:
        return os.access(path, os.W_OK)

    def reported_path(self, path: Path) -> str:
        relative = path.resolve().relative_to(self.mount_point)
        return str(self.reported_root / relative)

    def remove_cgroup(self, path: Path) -> None:
        path.rmdir()


def _parse_kv_counters(value: str, name: str) -> tuple[tuple[str, int], ...]:
    rows: list[tuple[str, int]] = []
    for line in value.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise ResourceEnforcementError(f"malformed {name} line")
        key, raw = fields
        try:
            number = int(raw)
        except ValueError as exc:
            raise ResourceEnforcementError(f"non-integer {name} counter") from exc
        if number < 0 or any(existing == key for existing, _ in rows):
            raise ResourceEnforcementError(f"invalid or duplicate {name} counter")
        rows.append((key, number))
    return tuple(sorted(rows))


def _counter(rows: tuple[tuple[str, int], ...], key: str, name: str) -> int:
    materialized = dict(rows)
    if key not in materialized:
        raise ResourceEnforcementError(f"{name} lacks required counter {key}")
    return materialized[key]


def _single_int(ops: CgroupV2Ops, path: Path, name: str) -> int:
    raw = ops.read(path).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ResourceEnforcementError(f"{name} is not an integer") from exc
    if value < 0:
        raise ResourceEnforcementError(f"{name} is negative")
    return value


@dataclass(frozen=True, slots=True)
class CgroupStats:
    memory_peak_bytes: int
    memory_current_bytes: int
    memory_swap_current_bytes: int
    pids_peak: int
    pids_current: int
    cpu_stat: tuple[tuple[str, int], ...]
    memory_events: tuple[tuple[str, int], ...]
    pids_events: tuple[tuple[str, int], ...]
    populated: bool

    def digest(self) -> str:
        return canonical_digest(asdict(self))


def _read_stats(ops: CgroupV2Ops, cgroup: Path) -> CgroupStats:
    cgroup_events = _parse_kv_counters(ops.read(cgroup / "cgroup.events"), "cgroup.events")
    return CgroupStats(
        memory_peak_bytes=_single_int(ops, cgroup / "memory.peak", "memory.peak"),
        memory_current_bytes=_single_int(
            ops, cgroup / "memory.current", "memory.current"
        ),
        memory_swap_current_bytes=_single_int(
            ops, cgroup / "memory.swap.current", "memory.swap.current"
        ),
        pids_peak=_single_int(ops, cgroup / "pids.peak", "pids.peak"),
        pids_current=_single_int(ops, cgroup / "pids.current", "pids.current"),
        cpu_stat=_parse_kv_counters(ops.read(cgroup / "cpu.stat"), "cpu.stat"),
        memory_events=_parse_kv_counters(
            ops.read(cgroup / "memory.events"), "memory.events"
        ),
        pids_events=_parse_kv_counters(
            ops.read(cgroup / "pids.events"), "pids.events"
        ),
        populated=bool(_counter(cgroup_events, "populated", "cgroup.events")),
    )


def _delta(after: int, before: int, name: str) -> int:
    if after < before:
        raise ResourceEnforcementError(f"cgroup counter moved backwards: {name}")
    return after - before


@dataclass(frozen=True, slots=True)
class GlobalLeaseEvidence:
    lease_id: str
    lock_path: str
    lock_inode: int
    acquired_epoch_ms: int
    released_epoch_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_object_from_text(value: str, name: str) -> dict[str, Any]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError(f"{name} must be bounded canonical JSON text")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is not JSON") from exc
    if not isinstance(decoded, dict) or _canonical_bytes(decoded).decode("utf-8") != value:
        raise ValueError(f"{name} is not a canonical JSON object")
    return decoded


@dataclass(frozen=True, slots=True)
class ExecutionRawEvidenceRecord:
    """Typed, immutable execution half of one cross-linked raw evidence pair."""

    schema: str
    execution_core_canonical_json: str
    execution_core_digest: str
    resource_core_digest: str
    pair_link_digest: str

    def __post_init__(self) -> None:
        if self.schema != "pok-execution-raw-record-v1":
            raise ValueError("unknown execution raw evidence schema")
        core = _canonical_object_from_text(
            self.execution_core_canonical_json, "execution raw core"
        )
        if core.get("schema") != "pok-execution-raw-core-v1":
            raise ValueError("execution raw record contains the wrong core schema")
        for name in (
            "execution_core_digest",
            "resource_core_digest",
            "pair_link_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if canonical_digest(core) != self.execution_core_digest:
            raise ValueError("execution raw core digest is stale")
        expected_link = canonical_digest(
            {
                "execution_core_digest": self.execution_core_digest,
                "resource_core_digest": self.resource_core_digest,
                "schema": "pok-execution-resource-raw-link-v1",
            }
        )
        if self.pair_link_digest != expected_link:
            raise ValueError("execution raw evidence has a stale pair link")

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_core": json.loads(self.execution_core_canonical_json),
            "execution_core_digest": self.execution_core_digest,
            "pair_link_digest": self.pair_link_digest,
            "resource_core_digest": self.resource_core_digest,
            "schema": self.schema,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ResourceRawEvidenceRecord:
    """Typed, immutable resource half of one cross-linked raw evidence pair."""

    schema: str
    resource_core_canonical_json: str
    resource_core_digest: str
    execution_core_digest: str
    pair_link_digest: str

    def __post_init__(self) -> None:
        if self.schema != "pok-resource-raw-record-v1":
            raise ValueError("unknown resource raw evidence schema")
        core = _canonical_object_from_text(
            self.resource_core_canonical_json, "resource raw core"
        )
        if core.get("schema") != "pok-resource-raw-core-v1":
            raise ValueError("resource raw record contains the wrong core schema")
        for name in (
            "resource_core_digest",
            "execution_core_digest",
            "pair_link_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if canonical_digest(core) != self.resource_core_digest:
            raise ValueError("resource raw core digest is stale")
        expected_link = canonical_digest(
            {
                "execution_core_digest": self.execution_core_digest,
                "resource_core_digest": self.resource_core_digest,
                "schema": "pok-execution-resource-raw-link-v1",
            }
        )
        if self.pair_link_digest != expected_link:
            raise ValueError("resource raw evidence has a stale pair link")

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_core_digest": self.execution_core_digest,
            "pair_link_digest": self.pair_link_digest,
            "resource_core": json.loads(self.resource_core_canonical_json),
            "resource_core_digest": self.resource_core_digest,
            "schema": self.schema,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())


def verify_raw_evidence_pair(
    execution: ExecutionRawEvidenceRecord,
    resource: ResourceRawEvidenceRecord,
) -> None:
    if not isinstance(execution, ExecutionRawEvidenceRecord) or not isinstance(
        resource, ResourceRawEvidenceRecord
    ):
        raise TypeError("raw evidence cross-link requires both typed record halves")
    if (
        execution.execution_core_digest != resource.execution_core_digest
        or execution.resource_core_digest != resource.resource_core_digest
        or execution.pair_link_digest != resource.pair_link_digest
    ):
        raise ValueError("execution/resource raw evidence records are not cross-linked")


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ConnectionEvidence:
    schema: str
    backend_kind: str
    formal_eligible: bool
    lease: GlobalLeaseEvidence
    leg_run_id: str
    leg_plan_digest: str
    profile_digest: str
    connection_index: int
    identity_digest: str
    launch_contract_digest: str
    launch_command_digest: str
    base_environment_digest: str
    thread_environment_digest: str
    launch_environment_digest: str
    artifact_materialization_digest: str
    actual_policy_seed: int
    run_id: str
    issuer_digest: str
    execution_verifier_digest: str
    process_pid: int
    process_group_id: int
    process_uid: int
    process_start_ticks: int
    cgroup_path: str
    cgroup_inode: int
    cgroup_mount_id: int
    controllers_digest: str
    enforcer_digest: str
    enforcer_source_sha256: str
    config_snapshot_digest: str
    cpu_affinity: tuple[int, ...]
    cpu_quota_us: int
    cpu_period_us: int
    max_tasks_limit: int
    memory_limit_bytes: int
    swap_limit_bytes: int
    gpu_devices: tuple[str, ...]
    vram_limit_bytes: int
    thread_environment: tuple[tuple[str, str], ...]
    cuda_visible_devices: str
    initial_stats_digest: str
    final_stats_digest: str
    observed_max_tasks: int
    observed_peak_memory_bytes: int
    observed_peak_swap_bytes: int
    observed_peak_vram_bytes: int
    oom_kill_count: int
    pids_limit_hit_count: int
    deadline_kill_count: int
    infrastructure_wall_kill_count: int
    cpu_throttled_usec: int
    started_epoch_ms: int
    finished_epoch_ms: int
    exit_code: int | None
    termination_kind: str
    timeout_kill_used_cgroup_kill: bool
    timeout_kill_used_process_group: bool
    empty_after_wait: bool
    cleanup_kill_used: bool
    cleanup_empty_confirmed: bool
    cleanup_child_removed: bool
    cleanup_receipt_digest: str
    cleanup_error: str | None
    decision_trace_digest: str
    decision_hard_stop_verified: bool
    no_pondering_verified: bool
    supervisor_leg_receipt_digest: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    def connection_evidence_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def _execution_core_record(self) -> dict[str, Any]:
        return {
            "artifact_materialization_digest": self.artifact_materialization_digest,
            "actual_policy_seed": self.actual_policy_seed,
            "base_environment_digest": self.base_environment_digest,
            "connection_index": self.connection_index,
            "decision_hard_stop_verified": self.decision_hard_stop_verified,
            "decision_trace_digest": self.decision_trace_digest,
            "empty_after_wait": self.empty_after_wait,
            "exit_code": self.exit_code,
            "finished_epoch_ms": self.finished_epoch_ms,
            "identity_digest": self.identity_digest,
            "infrastructure_wall_kill_count": self.infrastructure_wall_kill_count,
            "issuer_digest": self.issuer_digest,
            "launch_command_digest": self.launch_command_digest,
            "launch_contract_digest": self.launch_contract_digest,
            "launch_environment_digest": self.launch_environment_digest,
            "leg_plan_digest": self.leg_plan_digest,
            "leg_run_id": self.leg_run_id,
            "global_lease": self.lease.to_dict(),
            "no_pondering_verified": self.no_pondering_verified,
            "process_group_id": self.process_group_id,
            "process_pid": self.process_pid,
            "process_start_ticks": self.process_start_ticks,
            "process_uid": self.process_uid,
            "run_id": self.run_id,
            "schema": "pok-execution-raw-core-v1",
            "started_epoch_ms": self.started_epoch_ms,
            "termination_kind": self.termination_kind,
            "thread_environment_digest": self.thread_environment_digest,
            "execution_verifier_digest": self.execution_verifier_digest,
        }

    def _resource_core_record(self) -> dict[str, Any]:
        return {
            "cgroup_inode": self.cgroup_inode,
            "cgroup_mount_id": self.cgroup_mount_id,
            "cgroup_path": self.cgroup_path,
            "cleanup_child_removed": self.cleanup_child_removed,
            "cleanup_empty_confirmed": self.cleanup_empty_confirmed,
            "cleanup_error": self.cleanup_error,
            "cleanup_kill_used": self.cleanup_kill_used,
            "cleanup_receipt_digest": self.cleanup_receipt_digest,
            "config_snapshot_digest": self.config_snapshot_digest,
            "connection_index": self.connection_index,
            "controllers_digest": self.controllers_digest,
            "cpu_affinity": self.cpu_affinity,
            "cpu_period_us": self.cpu_period_us,
            "cpu_quota_us": self.cpu_quota_us,
            "cpu_throttled_usec": self.cpu_throttled_usec,
            "deadline_kill_count": self.deadline_kill_count,
            "enforcer_digest": self.enforcer_digest,
            "enforcer_source_sha256": self.enforcer_source_sha256,
            "final_stats_digest": self.final_stats_digest,
            "gpu_devices": self.gpu_devices,
            "identity_digest": self.identity_digest,
            "initial_stats_digest": self.initial_stats_digest,
            "max_tasks_limit": self.max_tasks_limit,
            "memory_limit_bytes": self.memory_limit_bytes,
            "observed_max_tasks": self.observed_max_tasks,
            "observed_peak_memory_bytes": self.observed_peak_memory_bytes,
            "observed_peak_swap_bytes": self.observed_peak_swap_bytes,
            "observed_peak_vram_bytes": self.observed_peak_vram_bytes,
            "oom_kill_count": self.oom_kill_count,
            "pids_limit_hit_count": self.pids_limit_hit_count,
            "profile_digest": self.profile_digest,
            "schema": "pok-resource-raw-core-v1",
            "swap_limit_bytes": self.swap_limit_bytes,
            "vram_limit_bytes": self.vram_limit_bytes,
        }

    def _raw_pair_link(self) -> tuple[str, str, str]:
        execution_core = canonical_digest(self._execution_core_record())
        resource_core = canonical_digest(self._resource_core_record())
        link = canonical_digest(
            {
                "execution_core_digest": execution_core,
                "resource_core_digest": resource_core,
                "schema": "pok-execution-resource-raw-link-v1",
            }
        )
        return execution_core, resource_core, link

    def execution_raw_record(self) -> ExecutionRawEvidenceRecord:
        execution_core, resource_core, link = self._raw_pair_link()
        return ExecutionRawEvidenceRecord(
            schema="pok-execution-raw-record-v1",
            execution_core_canonical_json=_canonical_bytes(
                self._execution_core_record()
            ).decode("utf-8"),
            execution_core_digest=execution_core,
            resource_core_digest=resource_core,
            pair_link_digest=link,
        )

    def resource_raw_record(self) -> ResourceRawEvidenceRecord:
        execution_core, resource_core, link = self._raw_pair_link()
        return ResourceRawEvidenceRecord(
            schema="pok-resource-raw-record-v1",
            resource_core_canonical_json=_canonical_bytes(
                self._resource_core_record()
            ).decode("utf-8"),
            resource_core_digest=resource_core,
            execution_core_digest=execution_core,
            pair_link_digest=link,
        )

    def execution_raw_evidence_digest(self) -> str:
        execution = self.execution_raw_record()
        resource = self.resource_raw_record()
        verify_raw_evidence_pair(execution, resource)
        return execution.digest()

    def resource_raw_evidence_digest(self) -> str:
        execution = self.execution_raw_record()
        resource = self.resource_raw_record()
        verify_raw_evidence_pair(execution, resource)
        return resource.digest()

    def termination_evidence_digest(self) -> str:
        return canonical_digest(
            {
                "deadline_kill_count": self.deadline_kill_count,
                "infrastructure_wall_kill_count": self.infrastructure_wall_kill_count,
                "empty_after_wait": self.empty_after_wait,
                "exit_code": self.exit_code,
                "oom_kill_count": self.oom_kill_count,
                "pids_limit_hit_count": self.pids_limit_hit_count,
                "schema": "pok-termination-evidence-v1",
                "termination_kind": self.termination_kind,
                "used_cgroup_kill": self.timeout_kill_used_cgroup_kill,
                "used_process_group": self.timeout_kill_used_process_group,
            }
        )

    def execution_receipt_kwargs(self) -> dict[str, Any]:
        return {
            "leg_plan_digest": self.leg_plan_digest,
            "identity_digest": self.identity_digest,
            "connection_index": self.connection_index,
            "launch_contract_digest": self.launch_contract_digest,
            "launch_command_digest": self.launch_command_digest,
            "base_environment_digest": self.base_environment_digest,
            "thread_environment_digest": self.thread_environment_digest,
            "launch_environment_digest": self.launch_environment_digest,
            "actual_policy_seed": self.actual_policy_seed,
            "run_id": self.run_id,
            "process_tree_id": f"pgid:{self.process_group_id}",
            "cgroup_path": self.cgroup_path,
            "issuer_digest": self.issuer_digest,
            "verifier_digest": self.execution_verifier_digest,
            "raw_evidence_digest": self.execution_raw_evidence_digest(),
            "termination_kind": self.termination_kind,
            "termination_evidence_digest": self.termination_evidence_digest(),
            "exit_code": self.exit_code,
        }

    def resource_receipt_kwargs(self, execution_receipt_digest: str) -> dict[str, Any]:
        return {
            "execution_receipt_digest": execution_receipt_digest,
            "profile_digest": self.profile_digest,
            "connection_index": self.connection_index,
            "identity_digest": self.identity_digest,
            "cgroup_path": self.cgroup_path,
            "cgroup_inode": self.cgroup_inode,
            "controllers_digest": self.controllers_digest,
            "enforcer_digest": self.enforcer_digest,
            "cpu_affinity": self.cpu_affinity,
            "cpu_quota_us": self.cpu_quota_us,
            "cpu_period_us": self.cpu_period_us,
            "max_tasks_limit": self.max_tasks_limit,
            "memory_limit_bytes": self.memory_limit_bytes,
            "swap_limit_bytes": self.swap_limit_bytes,
            "gpu_devices": self.gpu_devices,
            "vram_limit_bytes": self.vram_limit_bytes,
            "observed_max_tasks": self.observed_max_tasks,
            "observed_peak_rss_bytes": self.observed_peak_memory_bytes,
            "observed_peak_swap_bytes": self.observed_peak_swap_bytes,
            "observed_peak_vram_bytes": self.observed_peak_vram_bytes,
            "oom_kill_count": self.oom_kill_count,
            "pids_limit_hit_count": self.pids_limit_hit_count,
            "deadline_kill_count": self.deadline_kill_count,
            "cpu_throttled_usec": self.cpu_throttled_usec,
            "started_epoch_ms": self.started_epoch_ms,
            "finished_epoch_ms": self.finished_epoch_ms,
            "raw_evidence_digest": self.resource_raw_evidence_digest(),
            "verifier_digest": self.enforcer_digest,
            "cgroup_v2": True,
            "thermal_event": False,
            "host_preemption_event": False,
        }

    def to_formal_receipts(self) -> tuple[Any, Any]:
        # Formal evidence is a property of the complete signed two-connection
        # leg, not of either connection in isolation.  Keeping this public
        # compatibility method fail-closed prevents a caller from bypassing
        # AuthorizedSupervisorLeg's atomic one-shot conversion.
        raise FormalEnforcementUnavailable(
            "formal receipt emission requires the live external capability of the "
            "one-shot AuthorizedSupervisorLeg bridge"
        )


def _formal_receipts_from_authorized_connection(
    evidence: ConnectionEvidence,
    bridge: "AuthorizedSupervisorLeg",
) -> tuple[Any, Any]:
    """Build one pair only after the owning leg consumed its one-shot key."""

    bridge._assert_authorized()
    emission_key = (bridge.receipt_consumption_key, evidence.connection_index)
    if (
        bridge.receipt_consumption_key not in _CONVERTED_SUPERVISOR_BRIDGES
        or evidence not in bridge.connections
        or emission_key in _EMITTED_FORMAL_CONNECTION_RECEIPTS
        or not evidence.formal_eligible
        or evidence.backend_kind != SUPERVISOR_BACKEND_KIND
        or not evidence.cleanup_empty_confirmed
        or not evidence.cleanup_child_removed
        or evidence.cleanup_error is not None
        or evidence.supervisor_leg_receipt_digest is None
        or not evidence.decision_hard_stop_verified
        or not evidence.no_pondering_verified
        or not _has_formal_capability(evidence)
    ):
        raise FormalEnforcementUnavailable(
            "evidence lacks a live external-supervisor capability and verified cleanup"
        )
    _EMITTED_FORMAL_CONNECTION_RECEIPTS.add(emission_key)
    from .evaluation import ExecutionReceipt, ResourceReceipt, TerminationKind

    execution_kwargs = evidence.execution_receipt_kwargs()
    execution_kwargs["termination_kind"] = TerminationKind(evidence.termination_kind)
    execution = ExecutionReceipt(**execution_kwargs)
    sealed_execution_digest = execution.digest()

    def issued_execution(
        candidate: object,
        owner: object = execution,
        sealed: str = sealed_execution_digest,
    ) -> bool:
        return (
            candidate is owner
            and isinstance(candidate, ExecutionReceipt)
            and candidate.digest() == sealed
        )

    object.__setattr__(execution, "_enforcer_guard", issued_execution)
    resource = ResourceReceipt(
        **evidence.resource_receipt_kwargs(sealed_execution_digest)
    )
    sealed_resource_digest = resource.digest()

    def issued_resource(
        candidate: object,
        owner: object = resource,
        sealed: str = sealed_resource_digest,
    ) -> bool:
        return (
            candidate is owner
            and isinstance(candidate, ResourceReceipt)
            and candidate.digest() == sealed
        )

    object.__setattr__(resource, "_enforcer_guard", issued_resource)
    return execution, resource


@dataclass(frozen=True, slots=True)
class AuthorizedSupervisorLeg:
    """Verified bridge from signed post-run authority to replay/evaluation.

    Receipt-pair emission is process-local one-shot convenience.  Formal global
    uniqueness depends on the durable external ledgers exposed by this bridge.
    """

    connections: tuple[ConnectionEvidence, ConnectionEvidence]
    supervisor_contract_digest: str
    readiness_attestation_digest: str
    launch_authorization_digest: str
    supervisor_leg_receipt_digest: str
    attempt_journal_scope_digest: str
    attempt_sequence: int
    previous_attempt_entry_digest: str
    leg_plan_digest: str
    leg_run_id: str
    receipt_consumption_key: str
    consumption_ledger_entry_digest: str
    consumption_ledger_entry_inode: int
    consumption_ledger_entry_path: str
    control_session_digest: str
    capture_session_digest: str
    socket_identities: tuple[SocketCaptureIdentity, SocketCaptureIdentity]
    raw_wire_digest: str
    wire_semantic_digest: str
    replay_digest: str
    decision_events: tuple[DecisionEnforcementEvent, ...]
    decision_trace_digest: str
    supervisor_fault_events: tuple[DecisionEnforcementEvent, ...]
    supervisor_fault_event_digest: str
    termination_kinds: tuple[str, str]
    cleanup_receipt_digest: str
    _supervisor_contract: TrustedSupervisorContract = field(
        init=False, repr=False, compare=False
    )
    _readiness_attestation: SupervisorAttestation = field(
        init=False, repr=False, compare=False
    )
    _launch_authorization: SupervisorLaunchAuthorization = field(
        init=False, repr=False, compare=False
    )
    _leg_receipt: SupervisorLegReceipt = field(init=False, repr=False, compare=False)
    _consumption_ledger_entry: SupervisorConsumptionLedgerEntry = field(
        init=False, repr=False, compare=False
    )
    _authority_guard: object = field(init=False, repr=False, compare=False)

    def _assert_authorized(self) -> None:
        guard = getattr(self, "_authority_guard", None)
        if not callable(guard) or guard(self) is not True:
            raise FormalEnforcementUnavailable(
                "supervisor leg bridge was copied, altered, or not externally authorized"
            )
        contract = TrustedSupervisorContract.from_fixed_file()
        if contract.digest() != self.supervisor_contract_digest:
            raise FormalEnforcementUnavailable(
                "fixed supervisor contract changed after bridge authorization"
            )
        start_epoch = min(item.started_epoch_ms for item in self.connections)
        self._readiness_attestation.validate(contract, now_epoch_ms=start_epoch)
        self._launch_authorization.validate(contract, self._readiness_attestation)
        self._leg_receipt.validate(
            contract, self._readiness_attestation, self._launch_authorization
        )
        if (
            self._readiness_attestation.payload_digest()
            != self.readiness_attestation_digest
            or self._launch_authorization.payload_digest()
            != self.launch_authorization_digest
            or self._leg_receipt.payload_digest()
            != self.supervisor_leg_receipt_digest
            or self._consumption_ledger_entry.digest()
            != self.consumption_ledger_entry_digest
        ):
            raise FormalEnforcementUnavailable(
                "bridge signed-object identities differ from their sealed digests"
            )
        _verify_external_signature(contract, self._readiness_attestation)
        _verify_external_launch_signature(contract, self._launch_authorization)
        _verify_external_leg_signature(contract, self._leg_receipt)
        _verify_supervisor_process(self._readiness_attestation)
        _verify_supervisor_installation_identity(contract, self._readiness_attestation)
        ledger_entry, ledger_path = _verify_durable_consumption_ledger_entry(
            contract,
            self._readiness_attestation,
            self._launch_authorization,
            self._leg_receipt,
        )
        if (
            ledger_entry != self._consumption_ledger_entry
            or ledger_path != self.consumption_ledger_entry_path
        ):
            raise FormalEnforcementUnavailable(
                "durable consumption ledger changed after bridge authorization"
            )

    @property
    def raw_replay_digest(self) -> str:
        return self.replay_digest

    def formal_receipts(self) -> tuple[tuple[Any, Any], tuple[Any, Any]]:
        self._assert_authorized()
        if self.receipt_consumption_key in _CONVERTED_SUPERVISOR_BRIDGES:
            raise FormalEnforcementUnavailable(
                "authorized supervisor bridge already emitted its receipt pair"
            )
        # Consume locally before construction.  If receipt construction unexpectedly
        # fails, retrying the same externally signed leg stays fail-closed
        # instead of emitting a partial/replayed authority pair.
        _CONVERTED_SUPERVISOR_BRIDGES.add(self.receipt_consumption_key)
        receipts = tuple(  # type: ignore[assignment]
            _formal_receipts_from_authorized_connection(connection, self)
            for connection in self.connections
        )
        return receipts  # type: ignore[return-value]

    def replay_binding_payload(self) -> dict[str, Any]:
        self._assert_authorized()
        return {
            "attempt_journal_scope_digest": self.attempt_journal_scope_digest,
            "attempt_sequence": self.attempt_sequence,
            "supervisor_attempt_journal_scope_digest": self.attempt_journal_scope_digest,
            "supervisor_attempt_sequence": self.attempt_sequence,
            "capture_session_digest": self.capture_session_digest,
            "supervisor_capture_session_digest": self.capture_session_digest,
            "cleanup_receipt_digest": self.cleanup_receipt_digest,
            "consumption_ledger_entry_digest": self.consumption_ledger_entry_digest,
            "consumption_ledger_entry_inode": self.consumption_ledger_entry_inode,
            "consumption_ledger_entry_path": self.consumption_ledger_entry_path,
            "control_session_digest": self.control_session_digest,
            "supervisor_control_session_digest": self.control_session_digest,
            "decision_events": tuple(asdict(event) for event in self.decision_events),
            "supervisor_decision_events": tuple(
                asdict(event) for event in self.decision_events
            ),
            "decision_trace_digest": self.decision_trace_digest,
            "leg_plan_digest": self.leg_plan_digest,
            "leg_run_id": self.leg_run_id,
            "previous_attempt_entry_digest": self.previous_attempt_entry_digest,
            "supervisor_previous_attempt_entry_digest": self.previous_attempt_entry_digest,
            "readiness_attestation_digest": self.readiness_attestation_digest,
            "supervisor_readiness_attestation_digest": self.readiness_attestation_digest,
            "supervisor_contract_digest": self.supervisor_contract_digest,
            "supervisor_launch_authorization_digest": self.launch_authorization_digest,
            "supervisor_fault_event_digest": self.supervisor_fault_event_digest,
            "supervisor_fault_events": tuple(
                asdict(event) for event in self.supervisor_fault_events
            ),
            "termination_kinds": self.termination_kinds,
            "launch_authorization_digest": self.launch_authorization_digest,
            "raw_wire_digest": self.raw_wire_digest,
            "supervisor_raw_wire_digest": self.raw_wire_digest,
            "raw_replay_digest": self.raw_replay_digest,
            "receipt_consumption_key": self.receipt_consumption_key,
            "supervisor_receipt_consumption_key": self.receipt_consumption_key,
            "replay_digest": self.replay_digest,
            "supervisor_raw_replay_digest": self.raw_replay_digest,
            "socket_identities": tuple(asdict(item) for item in self.socket_identities),
            "supervisor_socket_identities": tuple(
                asdict(item) for item in self.socket_identities
            ),
            "socket_identity_digests": tuple(
                item.digest() for item in self.socket_identities
            ),
            "supervisor_socket_identity_digests": tuple(
                item.digest() for item in self.socket_identities
            ),
            "supervisor_leg_receipt_digest": self.supervisor_leg_receipt_digest,
            "supervisor_wire_semantic_digest": self.wire_semantic_digest,
            "wire_semantic_digest": self.wire_semantic_digest,
            "supervisor_decision_trace_digest": self.decision_trace_digest,
            "supervisor_cleanup_receipt_digest": self.cleanup_receipt_digest,
            "supervisor_termination_kinds": self.termination_kinds,
            "supervisor_consumption_ledger_entry_digest": self.consumption_ledger_entry_digest,
            "supervisor_consumption_ledger_entry_inode": self.consumption_ledger_entry_inode,
            "supervisor_consumption_ledger_entry_path": self.consumption_ledger_entry_path,
        }


def authorize_signed_supervisor_leg(
    *,
    connections: Sequence[ConnectionEvidence],
    readiness: SupervisorAttestation,
    launch_authorization: SupervisorLaunchAuthorization,
    leg_receipt: SupervisorLegReceipt,
    expected_probe_nonce: str,
    expected_launch_nonce: str,
    expected_capture_challenge: str,
    expected_capture_session_digest: str,
    expected_socket_identities: Sequence[SocketCaptureIdentity],
    expected_decision_events: Sequence[DecisionEnforcementEvent],
    expected_cleanup_receipt: "CleanupReceipt",
    expected_raw_wire_digest: str,
    expected_wire_semantic_digest: str,
    expected_replay_digest: str,
    authorization_epoch_ms: int,
) -> AuthorizedSupervisorLeg:
    """Verify external authority and grant process-local receipt conversion.

    There is intentionally no local ``mint`` helper.  The fixed root-owned
    contract and external signature verifier are reloaded here, and every
    ordered launch/resource/decision/cleanup field must match the signed leg
    receipt before the two exact object identities receive capability.  The
    in-memory replay check is defense in depth; callers must persistently
    consume ``receipt_consumption_key`` through the closed formal matrix ledger.
    """

    materialized = tuple(connections)
    if len(materialized) != 2 or tuple(
        item.connection_index for item in materialized
    ) != (0, 1):
        raise FormalEnforcementUnavailable(
            "signed supervisor leg requires ordered connection evidence 0,1"
        )
    contract = TrustedSupervisorContract.from_fixed_file()
    start_epoch = min(item.started_epoch_ms for item in materialized)
    readiness.validate(contract, now_epoch_ms=start_epoch)
    if readiness.nonce != expected_probe_nonce:
        raise FormalEnforcementUnavailable(
            "readiness attestation does not match the evaluator probe challenge"
        )
    _verify_external_signature(contract, readiness)
    launch_authorization.validate(contract, readiness)
    if launch_authorization.request_nonce != expected_launch_nonce:
        raise FormalEnforcementUnavailable(
            "launch authorization does not match the evaluator launch challenge"
        )
    if not all(
        launch_authorization.issued_epoch_ms
        <= item.started_epoch_ms
        <= launch_authorization.expires_epoch_ms
        for item in materialized
    ):
        raise FormalEnforcementUnavailable(
            "candidate processes did not start inside the signed prelaunch "
            "authorization interval"
        )
    _verify_external_launch_signature(contract, launch_authorization)
    leg_receipt.validate(contract, readiness, launch_authorization)
    if leg_receipt.capture_challenge != expected_capture_challenge:
        raise FormalEnforcementUnavailable(
            "post-run receipt does not match the evaluator capture challenge"
        )
    for actual, expected_value, name in (
        (
            leg_receipt.capture_session_digest,
            _digest(expected_capture_session_digest, "expected capture session"),
            "capture session",
        ),
        (
            leg_receipt.raw_wire_digest,
            _digest(expected_raw_wire_digest, "expected raw wire"),
            "raw wire",
        ),
        (
            leg_receipt.wire_semantic_digest,
            _digest(expected_wire_semantic_digest, "expected wire semantics"),
            "wire semantics",
        ),
        (
            leg_receipt.replay_digest,
            _digest(expected_replay_digest, "expected replay"),
            "replay",
        ),
    ):
        if actual != expected_value:
            raise FormalEnforcementUnavailable(
                f"post-run receipt differs from evaluator {name} evidence"
            )
    expected_sockets = tuple(expected_socket_identities)
    if (
        len(expected_sockets) != 2
        or tuple(item.connection_index for item in expected_sockets) != (0, 1)
        or tuple(item.digest() for item in expected_sockets)
        != tuple(item.digest() for item in leg_receipt.ordered_socket_identities)
    ):
        raise FormalEnforcementUnavailable(
            "post-run receipt differs from evaluator socket capture identities"
        )
    expected_events = tuple(expected_decision_events)
    if (
        not expected_events
        or expected_events != leg_receipt.decision_events
        or decision_trace_digest(expected_events) != leg_receipt.decision_trace_digest
    ):
        raise FormalEnforcementUnavailable(
            "post-run receipt differs from evaluator decision-open/action capture"
        )
    if (
        not isinstance(expected_cleanup_receipt, CleanupReceipt)
        or expected_cleanup_receipt.leg_run_id != leg_receipt.leg_run_id
        or not expected_cleanup_receipt.all_empty_and_removed
        or expected_cleanup_receipt.digest() != leg_receipt.cleanup_receipt_digest
    ):
        raise FormalEnforcementUnavailable(
            "post-run receipt differs from the complete durable cleanup receipt"
        )
    _strict_int(authorization_epoch_ms, "authorization time", minimum=1)
    if not (
        max(item.finished_epoch_ms for item in materialized)
        <= leg_receipt.issued_epoch_ms
        <= authorization_epoch_ms
        <= leg_receipt.issued_epoch_ms + 60_000
    ):
        raise FormalEnforcementUnavailable(
            "post-run receipt is stale or predates captured execution"
        )
    _verify_external_leg_signature(contract, leg_receipt)
    _verify_supervisor_process(readiness)
    _verify_supervisor_installation_identity(contract, readiness)
    ledger_entry, ledger_entry_path = _verify_durable_consumption_ledger_entry(
        contract, readiness, launch_authorization, leg_receipt
    )
    common_fields = (
        "leg_plan_digest",
        "leg_run_id",
        "profile_digest",
        "cgroup_mount_id",
        "decision_trace_digest",
        "cleanup_receipt_digest",
    )
    if any(
        getattr(materialized[0], name) != getattr(materialized[1], name)
        for name in common_fields
    ):
        raise FormalEnforcementUnavailable(
            "connection evidence does not describe one common supervised leg"
        )
    if materialized[0].lease != materialized[1].lease:
        raise FormalEnforcementUnavailable(
            "connection evidence does not share one global resource lease"
        )
    lease = materialized[0].lease
    if (
        lease.lock_path != str(FORMAL_GLOBAL_LOCK_PATH)
        or lease.lock_inode != readiness.global_lock_inode
        or lease.acquired_epoch_ms > start_epoch
        or lease.released_epoch_ms
        < max(item.finished_epoch_ms for item in materialized)
    ):
        raise FormalEnforcementUnavailable(
            "connection evidence lacks the attested fixed global lease interval"
        )
    expected = {
        "leg_plan_digest": materialized[0].leg_plan_digest,
        "leg_run_id": materialized[0].leg_run_id,
        "profile_digest": materialized[0].profile_digest,
        "ordered_identity_digests": tuple(
            item.identity_digest for item in materialized
        ),
        "ordered_materialization_digests": tuple(
            item.artifact_materialization_digest for item in materialized
        ),
        "ordered_launch_command_digests": tuple(
            item.launch_command_digest for item in materialized
        ),
        "ordered_base_environment_digests": tuple(
            item.base_environment_digest for item in materialized
        ),
        "ordered_launch_environment_digests": tuple(
            item.launch_environment_digest for item in materialized
        ),
        "ordered_policy_seeds": tuple(
            item.actual_policy_seed for item in materialized
        ),
        "ordered_process_pids": tuple(item.process_pid for item in materialized),
        "ordered_process_group_ids": tuple(
            item.process_group_id for item in materialized
        ),
        "ordered_process_start_ticks": tuple(
            item.process_start_ticks for item in materialized
        ),
        "ordered_process_uids": tuple(item.process_uid for item in materialized),
        "ordered_cgroup_paths": tuple(item.cgroup_path for item in materialized),
        "ordered_cgroup_inodes": tuple(item.cgroup_inode for item in materialized),
        "cgroup_mount_id": materialized[0].cgroup_mount_id,
        "execution_raw_record_digests": tuple(
            item.execution_raw_evidence_digest() for item in materialized
        ),
        "resource_raw_record_digests": tuple(
            item.resource_raw_evidence_digest() for item in materialized
        ),
        "ordered_issuer_digests": tuple(item.issuer_digest for item in materialized),
        "ordered_execution_verifier_digests": tuple(
            item.execution_verifier_digest for item in materialized
        ),
        "decision_trace_digest": materialized[0].decision_trace_digest,
        "cleanup_receipt_digest": materialized[0].cleanup_receipt_digest,
        "termination_kinds": tuple(item.termination_kind for item in materialized),
    }
    for name, value in expected.items():
        if getattr(leg_receipt, name) != value:
            raise FormalEnforcementUnavailable(
                f"signed supervisor leg differs from connection evidence: {name}"
            )
    receipt_digest = leg_receipt.payload_digest()
    if any(
        item.backend_kind != SUPERVISOR_BACKEND_KIND
        or not item.formal_eligible
        or item.supervisor_leg_receipt_digest != receipt_digest
        or item.enforcer_digest != RESOURCE_ENFORCER_DIGEST
        or item.decision_trace_digest != leg_receipt.decision_trace_digest
        or not item.decision_hard_stop_verified
        or not item.no_pondering_verified
        or not item.cleanup_empty_confirmed
        or not item.cleanup_child_removed
        or item.cleanup_error is not None
        for item in materialized
    ):
        raise FormalEnforcementUnavailable(
            "connection evidence lacks signed supervisor isolation/cleanup authority"
        )
    if len({item.cgroup_path for item in materialized}) != 2:
        raise FormalEnforcementUnavailable("candidate cgroup paths are not isolated")
    control_root = Path(contract.control_cgroup_root)
    if any(
        not Path(item.cgroup_path).is_relative_to(control_root)
        for item in materialized
    ) or any(item.cgroup_mount_id != readiness.control_cgroup_mount_id for item in materialized):
        raise FormalEnforcementUnavailable(
            "candidate cgroups are outside the attested supervisor-owned subtree"
        )
    if any(item.process_pid != item.process_group_id for item in materialized):
        raise FormalEnforcementUnavailable(
            "candidate process trees were not isolated with one setsid leader"
        )
    if leg_receipt.receipt_consumption_key in _CONSUMED_SUPERVISOR_RECEIPTS:
        raise FormalEnforcementUnavailable(
            "supervisor post-run receipt was already consumed in this evaluator process"
        )
    for evidence in materialized:
        identity = id(evidence)

        def discard(
            reference: weakref.ReferenceType[ConnectionEvidence],
            key: int = identity,
        ) -> None:
            current = _FORMAL_EVIDENCE_REGISTRY.get(key)
            if current is reference:
                _FORMAL_EVIDENCE_REGISTRY.pop(key, None)

        _FORMAL_EVIDENCE_REGISTRY[identity] = weakref.ref(evidence, discard)
    bridge = AuthorizedSupervisorLeg(
        connections=materialized,  # type: ignore[arg-type]
        supervisor_contract_digest=contract.digest(),
        readiness_attestation_digest=readiness.payload_digest(),
        launch_authorization_digest=launch_authorization.payload_digest(),
        supervisor_leg_receipt_digest=receipt_digest,
        attempt_journal_scope_digest=leg_receipt.attempt_journal_scope_digest,
        attempt_sequence=leg_receipt.attempt_sequence,
        previous_attempt_entry_digest=leg_receipt.previous_attempt_entry_digest,
        leg_plan_digest=leg_receipt.leg_plan_digest,
        leg_run_id=leg_receipt.leg_run_id,
        receipt_consumption_key=leg_receipt.receipt_consumption_key,
        consumption_ledger_entry_digest=leg_receipt.consumption_ledger_entry_digest,
        consumption_ledger_entry_inode=leg_receipt.consumption_ledger_entry_inode,
        consumption_ledger_entry_path=ledger_entry_path,
        control_session_digest=leg_receipt.control_session_digest,
        capture_session_digest=leg_receipt.capture_session_digest,
        socket_identities=leg_receipt.ordered_socket_identities,
        raw_wire_digest=leg_receipt.raw_wire_digest,
        wire_semantic_digest=leg_receipt.wire_semantic_digest,
        replay_digest=leg_receipt.replay_digest,
        decision_events=leg_receipt.decision_events,
        decision_trace_digest=leg_receipt.decision_trace_digest,
        supervisor_fault_events=tuple(
            event
            for event in leg_receipt.decision_events
            if event.fault_kind != "none"
        ),
        supervisor_fault_event_digest=decision_fault_trace_digest(
            leg_receipt.decision_events
        ),
        termination_kinds=leg_receipt.termination_kinds,
        cleanup_receipt_digest=leg_receipt.cleanup_receipt_digest,
    )
    object.__setattr__(bridge, "_supervisor_contract", contract)
    object.__setattr__(bridge, "_readiness_attestation", readiness)
    object.__setattr__(bridge, "_launch_authorization", launch_authorization)
    object.__setattr__(bridge, "_leg_receipt", leg_receipt)
    object.__setattr__(bridge, "_consumption_ledger_entry", ledger_entry)
    def bridge_seal_payload(candidate: AuthorizedSupervisorLeg) -> dict[str, Any]:
        return {
            "attempt_journal_scope_digest": candidate.attempt_journal_scope_digest,
            "attempt_sequence": candidate.attempt_sequence,
            "capture_session_digest": candidate.capture_session_digest,
            "cleanup_receipt_digest": candidate.cleanup_receipt_digest,
            "connection_evidence_digests": tuple(
                item.connection_evidence_digest() for item in candidate.connections
            ),
            "consumption_ledger_entry_digest": candidate.consumption_ledger_entry_digest,
            "consumption_ledger_entry_inode": candidate.consumption_ledger_entry_inode,
            "consumption_ledger_entry_path": candidate.consumption_ledger_entry_path,
            "control_session_digest": candidate.control_session_digest,
            "decision_event_digests": tuple(
                event.digest() for event in candidate.decision_events
            ),
            "decision_trace_digest": candidate.decision_trace_digest,
            "leg_plan_digest": candidate.leg_plan_digest,
            "leg_run_id": candidate.leg_run_id,
            "previous_attempt_entry_digest": candidate.previous_attempt_entry_digest,
            "readiness_attestation_digest": candidate.readiness_attestation_digest,
            "supervisor_contract_digest": candidate.supervisor_contract_digest,
            "supervisor_fault_event_digest": candidate.supervisor_fault_event_digest,
            "supervisor_fault_event_digests": tuple(
                event.digest() for event in candidate.supervisor_fault_events
            ),
            "termination_kinds": candidate.termination_kinds,
            "launch_authorization_digest": candidate.launch_authorization_digest,
            "raw_wire_digest": candidate.raw_wire_digest,
            "receipt_consumption_key": candidate.receipt_consumption_key,
            "replay_digest": candidate.replay_digest,
            "schema": "pok-authorized-supervisor-leg-bridge-v2",
            "socket_identity_digests": tuple(
                item.digest() for item in candidate.socket_identities
            ),
            "supervisor_leg_receipt_digest": candidate.supervisor_leg_receipt_digest,
            "wire_semantic_digest": candidate.wire_semantic_digest,
        }

    sealed_bridge = canonical_digest(bridge_seal_payload(bridge))

    def issued_bridge(
        candidate: object,
        owner: object = bridge,
        sealed: str = sealed_bridge,
    ) -> bool:
        if candidate is not owner or not isinstance(candidate, AuthorizedSupervisorLeg):
            return False
        return canonical_digest(bridge_seal_payload(candidate)) == sealed

    object.__setattr__(bridge, "_authority_guard", issued_bridge)
    _CONSUMED_SUPERVISOR_RECEIPTS.add(leg_receipt.receipt_consumption_key)
    return bridge


@dataclass(frozen=True, slots=True)
class CleanupPathOutcome:
    cgroup_path: str
    kill_attempted: bool
    empty_verified: bool
    remove_attempted: bool
    removed_verified: bool
    error: str | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cgroup_path, str)
            or not self.cgroup_path.startswith("/sys/fs/cgroup/")
            or ".." in Path(self.cgroup_path).parts
        ):
            raise ValueError("cleanup outcome has an invalid cgroup identity")
        for name in (
            "kill_attempted",
            "empty_verified",
            "remove_attempted",
            "removed_verified",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"cleanup {name} marker must be boolean")
        if self.removed_verified and (
            not self.empty_verified
            or not self.remove_attempted
            or self.error is not None
        ):
            raise ValueError("removed cleanup path lacks empty/remove proof")
        if self.error is not None and (
            not isinstance(self.error, str) or not self.error
        ):
            raise ValueError("cleanup error must be null or non-empty text")


@dataclass(frozen=True, slots=True)
class CleanupReceipt:
    schema: str
    leg_run_id: str
    trigger: str
    started_epoch_ms: int
    finished_epoch_ms: int
    path_outcomes: tuple[CleanupPathOutcome, ...]
    all_empty_and_removed: bool
    triggering_error_digest: str | None

    def __post_init__(self) -> None:
        if self.schema != "pok-resource-cleanup-receipt-v1":
            raise ValueError("unknown cleanup receipt schema")
        object.__setattr__(self, "leg_run_id", _digest(self.leg_run_id, "cleanup leg run"))
        if self.trigger not in {
            "normal",
            "configuration_failure",
            "launch_failure",
            "wait_failure",
            "evidence_failure",
            "cleanup_failure",
        }:
            raise ValueError("unknown cleanup trigger")
        _strict_int(self.started_epoch_ms, "cleanup start", minimum=1)
        _strict_int(self.finished_epoch_ms, "cleanup finish", minimum=1)
        if self.finished_epoch_ms < self.started_epoch_ms:
            raise ValueError("cleanup finish predates cleanup start")
        outcomes = tuple(self.path_outcomes)
        if len(outcomes) != 3 or len({item.cgroup_path for item in outcomes}) != 3:
            raise ValueError(
                "cleanup receipt requires exactly two child paths and one leg root"
            )
        if not outcomes[0].cgroup_path.endswith("/connection-0") or not outcomes[
            1
        ].cgroup_path.endswith("/connection-1"):
            raise ValueError("cleanup receipt child paths are not ordered 0,1")
        parent = str(Path(outcomes[0].cgroup_path).parent)
        if (
            str(Path(outcomes[1].cgroup_path).parent) != parent
            or outcomes[2].cgroup_path != parent
        ):
            raise ValueError("cleanup receipt paths do not share one leg root")
        object.__setattr__(self, "path_outcomes", outcomes)
        complete = all(
            item.empty_verified and item.remove_attempted and item.removed_verified
            and item.error is None
            for item in outcomes
        )
        if self.all_empty_and_removed is not complete:
            raise ValueError("cleanup completeness marker differs from path evidence")
        if self.trigger == "normal" and self.triggering_error_digest is not None:
            raise ValueError("normal cleanup cannot carry a triggering error")
        if self.trigger != "normal" and self.triggering_error_digest is None:
            raise ValueError("failure cleanup must bind the triggering error")
        if self.triggering_error_digest is not None:
            object.__setattr__(
                self,
                "triggering_error_digest",
                _digest(self.triggering_error_digest, "cleanup trigger error"),
            )

    def digest(self) -> str:
        return canonical_digest(asdict(self))

    def write_exclusive(self, path: str | os.PathLike[str]) -> None:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError("durable cleanup receipt path must be absolute")
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o400,
        )
        try:
            payload = _canonical_bytes(asdict(self))
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        directory = os.open(
            target.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


@dataclass(frozen=True, slots=True)
class LegEnforcementEvidence:
    schema: str
    lease: GlobalLeaseEvidence
    leg_run_id: str
    connections: tuple[ConnectionEvidence, ConnectionEvidence]
    cleanup_receipt: CleanupReceipt
    cleanup_parent_removed: bool
    cleanup_error: str | None

    def __post_init__(self) -> None:
        if self.schema != "pok-resource-leg-evidence-v1":
            raise ValueError("unknown leg enforcement evidence schema")
        object.__setattr__(self, "leg_run_id", _digest(self.leg_run_id, "leg run ID"))
        connections = tuple(self.connections)
        if (
            len(connections) != 2
            or tuple(item.connection_index for item in connections) != (0, 1)
            or any(item.leg_run_id != self.leg_run_id for item in connections)
        ):
            raise ValueError("leg evidence requires ordered connections from one run")
        object.__setattr__(self, "connections", connections)
        cleanup_digest = self.cleanup_receipt.digest()
        if any(
            item.cleanup_receipt_digest != cleanup_digest for item in connections
        ):
            raise ValueError("connection evidence is not bound to the cleanup receipt")
        if self.cleanup_parent_removed != self.cleanup_receipt.all_empty_and_removed:
            raise ValueError("leg cleanup marker differs from the durable receipt")
        if self.cleanup_error is None and not self.cleanup_parent_removed:
            raise ValueError("incomplete cleanup must carry an error")
        if self.cleanup_error is not None and self.cleanup_parent_removed:
            raise ValueError("complete cleanup cannot carry an error")

    def to_dict(self) -> dict[str, Any]:
        return {
            "connections": [item.to_dict() for item in self.connections],
            "lease": self.lease.to_dict(),
            "leg_run_id": self.leg_run_id,
            "cleanup_receipt": asdict(self.cleanup_receipt),
            "cleanup_parent_removed": self.cleanup_parent_removed,
            "cleanup_error": self.cleanup_error,
            "schema": self.schema,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def write_exclusive(self, path: str | os.PathLike[str]) -> None:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError("durable leg evidence path must be absolute")
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o444,
        )
        try:
            payload = self.canonical_bytes()
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        directory = os.open(
            target.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


@dataclass(slots=True)
class _LeaseHandle:
    descriptor: int
    lease_id: str
    path: Path
    inode: int
    acquired_epoch_ms: int
    released_epoch_ms: int | None = None


def _open_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )


@contextmanager
def global_sequential_lease(
    lock_path: str | os.PathLike[str] = FORMAL_GLOBAL_LOCK_PATH,
    *,
    timeout_sec: float = 30.0,
    _allow_diagnostic_path: bool = False,
) -> Iterator[_LeaseHandle]:
    if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float)) or timeout_sec < 0:
        raise ValueError("lease timeout must be nonnegative")
    path = Path(lock_path)
    if path != FORMAL_GLOBAL_LOCK_PATH and not _allow_diagnostic_path:
        raise FormalEnforcementUnavailable(
            "formal global lease path is fixed and cannot be selected by the caller"
        )
    descriptor = _open_lock(path)
    deadline = time.monotonic() + timeout_sec
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GlobalLeaseTimeout("global formal resource lease is busy")
                time.sleep(0.01)
        metadata = os.fstat(descriptor)
        handle = _LeaseHandle(
            descriptor=descriptor,
            lease_id=secrets.token_hex(16),
            path=path.resolve(),
            inode=metadata.st_ino,
            acquired_epoch_ms=time.time_ns() // 1_000_000,
        )
        yield handle
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            # This is the earliest trustworthy userspace timestamp after the
            # kernel accepted LOCK_UN.  Evidence is built only after the
            # context exits.
            if "handle" in locals():
                handle.released_epoch_ms = max(
                    time.time_ns() // 1_000_000,
                    handle.acquired_epoch_ms + 1,
                )
        finally:
            os.close(descriptor)


@dataclass(slots=True)
class _RunningConnection:
    binding: ExecutionBinding
    cgroup: Path
    cgroup_path: str
    inode: int
    config_digest: str
    initial_stats: CgroupStats
    process: subprocess.Popen[bytes]
    started_epoch_ms: int
    process_uid: int
    process_start_ticks: int
    timed_out: bool = False
    infrastructure_wall_stopped: bool = False
    used_cgroup_kill: bool = False
    used_process_group: bool = False


class FormalResourceEnforcer:
    """Deprecated same-UID diagnostic runner; it is never formal authority.

    Formal launches are owned exclusively by the installed external privileged
    supervisor described by :class:`TrustedSupervisorContract`.  This class
    accepts only explicitly injected non-formal backends and cannot mint formal
    evaluation receipts.
    """

    def __init__(
        self,
        *,
        delegated_root: str | os.PathLike[str],
        lock_path: str | os.PathLike[str],
        profile: EnforcementProfile,
        lease_timeout_sec: float = 30.0,
        _ops: CgroupV2Ops | None = None,
        _allow_test_backend: bool = False,
        _test_wall_timeout_ms: int | None = None,
    ) -> None:
        profile.validate()
        self.root = Path(os.path.abspath(os.fspath(delegated_root)))
        if self.root.resolve(strict=False) != self.root:
            raise FormalEnforcementUnavailable(
                "delegated cgroup root cannot traverse a symlink"
            )
        self.lock_path = Path(lock_path)
        self.profile = profile
        self.lease_timeout_sec = lease_timeout_sec
        if _ops is None:
            raise FormalEnforcementUnavailable(
                "the same-UID cgroup launcher is diagnostic only; formal execution "
                "requires the fixed external privileged supervisor"
            )
        if _test_wall_timeout_ms is not None:
            _strict_int(_test_wall_timeout_ms, "test wall timeout", minimum=1)
            if _ops is None or not _allow_test_backend:
                raise FormalEnforcementUnavailable(
                    "wall-time override is available only to a non-formal injected backend"
                )
        self._wall_timeout_ms = (
            profile.match_wall_timeout_ms
            if _test_wall_timeout_ms is None
            else _test_wall_timeout_ms
        )
        if not _allow_test_backend:
            raise FormalEnforcementUnavailable(
                "diagnostic cgroup backends require explicit non-formal opt-in"
            )
        if getattr(_ops, "formal_eligible", True):
            raise FormalEnforcementUnavailable(
                "an in-process backend may never claim formal eligibility"
            )
        self.ops = _ops
        self._poisoned_cleanup_receipt: CleanupReceipt | None = None
        self.last_cleanup_receipt: CleanupReceipt | None = None
        self._validate_delegation()

    def _validate_delegation(self) -> None:
        if not self.root.is_dir() or self.root.is_symlink():
            raise FormalEnforcementUnavailable("delegated cgroup root is not a real directory")
        for filename in REQUIRED_CONTROLLER_FILES:
            try:
                self.ops.read(self.root / filename)
            except (OSError, ResourceEnforcementError) as exc:
                raise FormalEnforcementUnavailable(
                    f"delegated root lacks readable {filename}"
                ) from exc
        controllers = set(self.ops.read(self.root / "cgroup.controllers").split())
        if not set(REQUIRED_CONTROLLERS) <= controllers:
            raise FormalEnforcementUnavailable("delegated root lacks required controllers")
        if self.ops.read(self.root / "cgroup.type").strip() != "domain":
            raise FormalEnforcementUnavailable("delegated root must be a domain cgroup")
        if self.ops.read(self.root / "cgroup.procs").strip():
            raise FormalEnforcementUnavailable("delegated root contains internal processes")
        effective_cpus = set(
            _parse_cpuset(self.ops.read(self.root / "cpuset.cpus.effective"))
        )
        requested = set(self.profile.cpu_affinity_by_connection[0]) | set(
            self.profile.cpu_affinity_by_connection[1]
        )
        if not requested <= effective_cpus:
            raise FormalEnforcementUnavailable("requested CPU set exceeds delegated cpuset")
        if not self.ops.read(self.root / "cpuset.mems.effective").strip():
            raise FormalEnforcementUnavailable("delegated NUMA memory set is empty")
        for path in (
            self.root,
            self.root / "cgroup.procs",
            self.root / "cgroup.subtree_control",
        ):
            if not self.ops.is_writable(path):
                raise FormalEnforcementUnavailable(f"delegated cgroup path is not writable: {path}")

    def _write_and_require(self, path: Path, value: str, expected: str | None = None) -> str:
        self.ops.write(path, value)
        observed = self.ops.read(path).strip()
        target = value if expected is None else expected
        if observed != target:
            raise ResourceEnforcementError(
                f"cgroup control readback mismatch for {path.name}: {observed!r} != {target!r}"
            )
        return observed

    def _enable_controllers(self, cgroup: Path) -> None:
        requested = " ".join(f"+{name}" for name in REQUIRED_CONTROLLERS)
        self.ops.write(cgroup / "cgroup.subtree_control", requested)
        enabled = set(self.ops.read(cgroup / "cgroup.subtree_control").split())
        if not set(REQUIRED_CONTROLLERS) <= enabled:
            raise ResourceEnforcementError("required cgroup controllers were not enabled")

    def _configure_leg_cgroups(
        self, spec: LegLaunchSpec
    ) -> tuple[Path, tuple[tuple[Path, str, int, str, CgroupStats], ...]]:
        self._enable_controllers(self.root)
        leg_root = self.root / f"leg-{spec.leg_run_id}"
        self.ops.create_cgroup(leg_root)
        mems = self.ops.read(self.root / "cpuset.mems.effective").strip()
        union = tuple(
            sorted(
                set(self.profile.cpu_affinity_by_connection[0])
                | set(self.profile.cpu_affinity_by_connection[1])
            )
        )
        self._write_and_require(leg_root / "cpuset.mems", mems)
        self._write_and_require(leg_root / "cpuset.cpus", _format_cpuset(union))
        self._enable_controllers(leg_root)
        configured = []
        for connection in range(2):
            cgroup = leg_root / f"connection-{connection}"
            self.ops.create_cgroup(cgroup)
            affinity = self.profile.cpu_affinity_by_connection[connection]
            config = {
                "cpu.max": f"{self.profile.cpu_quota_us} {self.profile.cpu_period_us}",
                "cpuset.cpus": _format_cpuset(affinity),
                "cpuset.mems": mems,
                "memory.max": str(self.profile.ram_limit_bytes_per_connection),
                "memory.oom.group": "1",
                "memory.swap.max": "0",
                "pids.max": str(self.profile.max_tasks_per_connection),
            }
            observed_config: dict[str, str] = {}
            for filename, value in config.items():
                observed_config[filename] = self._write_and_require(
                    cgroup / filename, value
                )
            missing = [name for name in REQUIRED_CHILD_FILES if not (cgroup / name).exists()]
            if missing:
                raise ResourceEnforcementError(
                    f"cgroup child lacks required files: {','.join(missing)}"
                )
            inode = self.ops.inode(cgroup)
            reported = self.ops.reported_path(cgroup)
            if not reported.startswith("/sys/fs/cgroup/"):
                raise ResourceEnforcementError("reported cgroup path is not under /sys/fs/cgroup")
            snapshot = {
                "backend": self.ops.backend_kind,
                "cgroup_inode": inode,
                "cgroup_path": reported,
                "config_readback": observed_config,
                "controllers_digest": self.profile.controllers_digest,
                "enforcer_digest": self.profile.enforcer_digest,
                "enforcer_source_sha256": RESOURCE_ENFORCER_SOURCE_SHA256,
                "mount_id": self.ops.mount_id,
                "profile_digest": self.profile.profile_digest,
                "schema": "pok-cgroup-config-snapshot-v1",
                "thread_environment_digest": self.profile.thread_environment_digest,
            }
            initial = _read_stats(self.ops, cgroup)
            if initial.populated or initial.pids_current != 0:
                raise ResourceEnforcementError("new cgroup is unexpectedly populated")
            configured.append((cgroup, reported, inode, canonical_digest(snapshot), initial))
        return leg_root, tuple(configured)

    def _launch(
        self,
        binding: ExecutionBinding,
        cgroup: Path,
        cgroup_path: str,
        inode: int,
        config_digest: str,
        initial_stats: CgroupStats,
    ) -> _RunningConnection:
        materialized_now = verify_artifact_materialization(
            binding.artifact_expectation
        )
        if (
            materialized_now.identity_digest != binding.identity_digest
            or materialized_now.root_device != binding.artifact_root_device
            or materialized_now.root_inode != binding.artifact_root_inode
            or materialized_now.executable_device != binding.executable_device
            or materialized_now.executable_inode != binding.executable_inode
        ):
            raise ResourceEnforcementError(
                "artifact materialization changed between binding and launch"
            )
        effective = _effective_environment(
            self.profile,
            binding.connection_index,
            binding.actual_policy_seed,
            dict(binding.base_environment),
        )
        if launch_command_digest(binding.argv, binding.cwd) != binding.launch_command_digest:
            raise ResourceEnforcementError("launch command differs from its frozen digest")
        if launch_environment_digest(effective) != binding.launch_environment_digest:
            raise ResourceEnforcementError("launch environment differs from its frozen digest")
        if not Path(binding.cwd).is_dir():
            raise ResourceEnforcementError("launch working directory is unavailable")

        ops = self.ops

        def attach_before_exec() -> None:
            ops.write(cgroup / "cgroup.procs", str(os.getpid()))

        started = time.time_ns() // 1_000_000
        process = subprocess.Popen(
            binding.argv,
            cwd=binding.cwd,
            env=effective,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=attach_before_exec,
            close_fds=True,
        )
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            pgid = process.pid
        if pgid != process.pid:
            process.kill()
            process.wait()
            raise ResourceEnforcementError("setsid did not create an isolated process group")
        try:
            process_uid = Path(f"/proc/{process.pid}").stat().st_uid
            process_start_ticks = int(
                Path(f"/proc/{process.pid}/stat").read_text(encoding="ascii").split()[21]
            )
        except (OSError, ValueError, IndexError) as exc:
            process.kill()
            process.wait()
            raise ResourceEnforcementError(
                "could not bind launched process uid/start ticks"
            ) from exc
        return _RunningConnection(
            binding=binding,
            cgroup=cgroup,
            cgroup_path=cgroup_path,
            inode=inode,
            config_digest=config_digest,
            initial_stats=initial_stats,
            process=process,
            started_epoch_ms=started,
            process_uid=process_uid,
            process_start_ticks=process_start_ticks,
        )

    def _kill_tree(self, running: _RunningConnection, *, reason: str) -> None:
        try:
            self.ops.write(running.cgroup / "cgroup.kill", "1")
            running.used_cgroup_kill = True
        except OSError:
            pass
        try:
            os.killpg(running.process.pid, signal.SIGKILL)
            running.used_process_group = True
        except ProcessLookupError:
            pass
        if reason == "decision_timeout":
            running.timed_out = True
        elif reason == "infrastructure_wall":
            running.infrastructure_wall_stopped = True
        elif reason != "cleanup":
            raise ValueError("unknown process-tree termination reason")

    def _wait_all(self, running: Sequence[_RunningConnection]) -> None:
        deadline = time.monotonic() + self._wall_timeout_ms / 1000.0
        while any(
            item.process.poll() is None
            or bool(self.ops.read(item.cgroup / "cgroup.procs").strip())
            for item in running
        ):
            if time.monotonic() >= deadline:
                for item in running:
                    self._kill_tree(item, reason="infrastructure_wall")
                break
            time.sleep(0.005)
        for item in running:
            try:
                item.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_tree(item, reason="infrastructure_wall")
                item.process.wait(timeout=5)

    def _connection_evidence(
        self,
        running: _RunningConnection,
        lease: GlobalLeaseEvidence,
        spec: LegLaunchSpec,
    ) -> ConnectionEvidence:
        final = _read_stats(self.ops, running.cgroup)
        finished = max(time.time_ns() // 1_000_000, running.started_epoch_ms + 1)
        oom_kill = _delta(
            _counter(final.memory_events, "oom_kill", "memory.events"),
            _counter(running.initial_stats.memory_events, "oom_kill", "memory.events"),
            "memory.events oom_kill",
        )
        pids_max = _delta(
            _counter(final.pids_events, "max", "pids.events"),
            _counter(running.initial_stats.pids_events, "max", "pids.events"),
            "pids.events max",
        )
        throttled = _delta(
            _counter(final.cpu_stat, "throttled_usec", "cpu.stat"),
            _counter(running.initial_stats.cpu_stat, "throttled_usec", "cpu.stat"),
            "cpu.stat throttled_usec",
        )
        exit_code = running.process.returncode
        if running.timed_out:
            termination = "timeout"
        elif running.infrastructure_wall_stopped:
            termination = "infrastructure"
        elif exit_code == 0:
            termination = "normal"
        elif oom_kill or pids_max:
            termination = "resource"
        else:
            termination = "crash"
        empty = not final.populated and final.pids_current == 0
        return ConnectionEvidence(
            schema="pok-resource-connection-evidence-v1",
            backend_kind=self.ops.backend_kind,
            formal_eligible=False,
            lease=lease,
            leg_run_id=spec.leg_run_id,
            leg_plan_digest=spec.leg_plan_digest,
            profile_digest=self.profile.profile_digest,
            connection_index=running.binding.connection_index,
            identity_digest=running.binding.identity_digest,
            launch_contract_digest=running.binding.launch_contract_digest,
            launch_command_digest=running.binding.launch_command_digest,
            base_environment_digest=running.binding.base_environment_digest,
            thread_environment_digest=running.binding.thread_environment_digest,
            launch_environment_digest=running.binding.launch_environment_digest,
            artifact_materialization_digest=running.binding.artifact_materialization_digest,
            actual_policy_seed=running.binding.actual_policy_seed,
            run_id=running.binding.run_id,
            issuer_digest=running.binding.issuer_digest,
            execution_verifier_digest=running.binding.verifier_digest,
            process_pid=running.process.pid,
            process_group_id=running.process.pid,
            process_uid=running.process_uid,
            process_start_ticks=running.process_start_ticks,
            cgroup_path=running.cgroup_path,
            cgroup_inode=running.inode,
            cgroup_mount_id=self.ops.mount_id,
            controllers_digest=self.profile.controllers_digest,
            enforcer_digest=self.profile.enforcer_digest,
            enforcer_source_sha256=RESOURCE_ENFORCER_SOURCE_SHA256,
            config_snapshot_digest=running.config_digest,
            cpu_affinity=self.profile.cpu_affinity_by_connection[
                running.binding.connection_index
            ],
            cpu_quota_us=self.profile.cpu_quota_us,
            cpu_period_us=self.profile.cpu_period_us,
            max_tasks_limit=self.profile.max_tasks_per_connection,
            memory_limit_bytes=self.profile.ram_limit_bytes_per_connection,
            swap_limit_bytes=0,
            gpu_devices=self.profile.gpu_devices_by_connection[
                running.binding.connection_index
            ],
            vram_limit_bytes=self.profile.vram_limit_bytes_per_connection,
            thread_environment=self.profile.thread_environment,
            cuda_visible_devices=",".join(
                self.profile.gpu_devices_by_connection[
                    running.binding.connection_index
                ]
            ),
            initial_stats_digest=running.initial_stats.digest(),
            final_stats_digest=final.digest(),
            observed_max_tasks=final.pids_peak,
            observed_peak_memory_bytes=final.memory_peak_bytes,
            observed_peak_swap_bytes=final.memory_swap_current_bytes,
            observed_peak_vram_bytes=0,
            oom_kill_count=oom_kill,
            pids_limit_hit_count=pids_max,
            deadline_kill_count=int(running.timed_out),
            infrastructure_wall_kill_count=int(running.infrastructure_wall_stopped),
            cpu_throttled_usec=throttled,
            started_epoch_ms=running.started_epoch_ms,
            finished_epoch_ms=finished,
            exit_code=exit_code,
            termination_kind=termination,
            timeout_kill_used_cgroup_kill=running.used_cgroup_kill,
            timeout_kill_used_process_group=running.used_process_group,
            empty_after_wait=empty,
            cleanup_kill_used=False,
            cleanup_empty_confirmed=False,
            cleanup_child_removed=False,
            cleanup_receipt_digest=canonical_digest(
                {
                    "leg_run_id": spec.leg_run_id,
                    "schema": "pok-cleanup-receipt-pending-v1",
                }
            ),
            cleanup_error=None,
            decision_trace_digest=decision_trace_digest(()),
            decision_hard_stop_verified=False,
            no_pondering_verified=False,
            supervisor_leg_receipt_digest=None,
        )

    def _cleanup_connection(
        self, running: _RunningConnection
    ) -> tuple[bool, bool, bool, str | None]:
        kill_used = False
        try:
            stats = _read_stats(self.ops, running.cgroup)
            processes = self.ops.read(running.cgroup / "cgroup.procs").strip()
            if stats.populated or stats.pids_current or processes:
                self.ops.write(running.cgroup / "cgroup.kill", "1")
                kill_used = True
                try:
                    os.killpg(running.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 5.0
            while True:
                stats = _read_stats(self.ops, running.cgroup)
                processes = self.ops.read(running.cgroup / "cgroup.procs").strip()
                if not stats.populated and stats.pids_current == 0 and not processes:
                    break
                if time.monotonic() >= deadline:
                    return kill_used, False, False, "cgroup remained populated after kill"
                time.sleep(0.01)
            self.ops.remove_cgroup(running.cgroup)
            return kill_used, True, not running.cgroup.exists(), None
        except Exception as exc:
            return kill_used, False, False, f"{type(exc).__name__}: {exc}"

    def _best_effort_cleanup_partial_leg(self, leg_root: Path) -> None:
        for connection in (1, 0):
            child = leg_root / f"connection-{connection}"
            if not child.exists():
                continue
            try:
                self.ops.write(child / "cgroup.kill", "1")
            except Exception:
                pass
            try:
                self.ops.remove_cgroup(child)
            except Exception:
                pass
        if leg_root.exists():
            try:
                self.ops.remove_cgroup(leg_root)
            except Exception:
                pass

    def run_leg(self, spec: LegLaunchSpec) -> LegEnforcementEvidence:
        # Recheck immediately before acquiring authority: a source/config drift
        # after object construction must fail before any cgroup is created.
        self.profile.validate()
        with global_sequential_lease(
            self.lock_path,
            timeout_sec=self.lease_timeout_sec,
            _allow_diagnostic_path=True,
        ) as lease_handle:
            leg_root = self.root / f"leg-{spec.leg_run_id}"
            try:
                leg_root, configured = self._configure_leg_cgroups(spec)
            except BaseException:
                self._best_effort_cleanup_partial_leg(leg_root)
                raise
            running: list[_RunningConnection] = []
            try:
                for binding, config in zip(spec.bindings, configured, strict=True):
                    running.append(self._launch(binding, *config))
                self._wait_all(running)
            except BaseException:
                for item in running:
                    self._kill_tree(item, reason="cleanup")
                    try:
                        item.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                self._best_effort_cleanup_partial_leg(leg_root)
                raise
            lease = GlobalLeaseEvidence(
                lease_id=lease_handle.lease_id,
                lock_path=str(lease_handle.path),
                lock_inode=lease_handle.inode,
                acquired_epoch_ms=lease_handle.acquired_epoch_ms,
                released_epoch_ms=lease_handle.acquired_epoch_ms + 1,
            )
            try:
                connections = tuple(
                    self._connection_evidence(item, lease, spec) for item in running
                )
            except BaseException:
                for item in running:
                    self._cleanup_connection(item)
                self._best_effort_cleanup_partial_leg(leg_root)
                raise
            cleanup_started = time.time_ns() // 1_000_000
            cleaned: list[ConnectionEvidence] = []
            cleanup_outcomes: list[CleanupPathOutcome] = []
            cleanup_errors: list[str] = []
            for item, connection in zip(running, connections, strict=True):
                kill_used, empty, removed, error = self._cleanup_connection(item)
                if error is not None:
                    cleanup_errors.append(
                        f"connection-{item.binding.connection_index}: {error}"
                    )
                cleaned.append(
                    replace(
                        connection,
                        cleanup_kill_used=kill_used,
                        cleanup_empty_confirmed=empty,
                        cleanup_child_removed=removed,
                        cleanup_error=error,
                    )
                )
                cleanup_outcomes.append(
                    CleanupPathOutcome(
                        cgroup_path=item.cgroup_path,
                        kill_attempted=kill_used,
                        empty_verified=empty,
                        remove_attempted=True,
                        removed_verified=removed,
                        error=error,
                    )
                )
            parent_removed = False
            parent_error: str | None = None
            parent_path = self.ops.reported_path(leg_root)
            if not cleanup_errors:
                try:
                    self.ops.remove_cgroup(leg_root)
                    parent_removed = not leg_root.exists()
                    if not parent_removed:
                        raise ResourceEnforcementError("leg cgroup still exists after removal")
                except Exception as exc:
                    parent_error = f"{type(exc).__name__}: {exc}"
                    cleanup_errors.append(f"leg-parent: {parent_error}")
            else:
                parent_error = "child cleanup failed; parent removal was not attempted"
            cleanup_outcomes.append(
                CleanupPathOutcome(
                    cgroup_path=parent_path,
                    kill_attempted=False,
                    empty_verified=parent_removed,
                    remove_attempted=not any(
                        item.error is not None for item in cleanup_outcomes
                    ),
                    removed_verified=parent_removed,
                    error=parent_error,
                )
            )
            cleanup_error_text = (
                "; ".join(cleanup_errors) if cleanup_errors else None
            )
            cleanup_receipt = CleanupReceipt(
                schema="pok-resource-cleanup-receipt-v1",
                leg_run_id=spec.leg_run_id,
                trigger="cleanup_failure" if cleanup_errors else "normal",
                started_epoch_ms=cleanup_started,
                finished_epoch_ms=max(
                    time.time_ns() // 1_000_000, cleanup_started + 1
                ),
                path_outcomes=tuple(cleanup_outcomes),
                all_empty_and_removed=not cleanup_errors and parent_removed,
                triggering_error_digest=(
                    canonical_digest(
                        {
                            "cleanup_error": cleanup_error_text,
                            "leg_run_id": spec.leg_run_id,
                            "schema": "pok-diagnostic-cleanup-error-v1",
                        }
                    )
                    if cleanup_errors
                    else None
                ),
            )
            lease = replace(
                lease,
                released_epoch_ms=max(
                    time.time_ns() // 1_000_000,
                    lease_handle.acquired_epoch_ms + 1,
                ),
            )
            cleaned = [
                replace(
                    connection,
                    lease=lease,
                    cleanup_receipt_digest=cleanup_receipt.digest(),
                )
                for connection in cleaned
            ]
            evidence = LegEnforcementEvidence(
                schema="pok-resource-leg-evidence-v1",
                lease=lease,
                leg_run_id=spec.leg_run_id,
                connections=tuple(cleaned),  # type: ignore[arg-type]
                cleanup_receipt=cleanup_receipt,
                cleanup_parent_removed=parent_removed,
                cleanup_error=cleanup_error_text,
            )
            evidence.canonical_bytes()
            if cleanup_errors:
                raise ResourceCleanupError(
                    "diagnostic cgroup cleanup failed: " + "; ".join(cleanup_errors),
                    evidence,
                )
            return evidence


# Honest name for new callers.  The legacy class name remains available so old
# diagnostic code fails closed instead of accidentally selecting a new formal
# path.
DiagnosticCgroupRunner = FormalResourceEnforcer
