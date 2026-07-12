"""Fail-closed central process boundary for managed native bots and workers.

The host owns one exact TCP stream and all launch-time descriptors; children
receive explicit mounts/env only, isolated namespaces, and no INET socket API.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
from typing import IO, Mapping, Sequence

from managed_bot_socket import (
    PRECONNECTED_FD_ENV,
    PRECONNECTED_HOST_ENV,
    PRECONNECTED_PORT_ENV,
    SANDBOX_BOOTSTRAP,
)


class ManagedExecutorError(RuntimeError):
    """The requested managed execution cannot satisfy its safety contract."""


class IsolationUnavailable(ManagedExecutorError):
    """The host lacks a mandatory isolation primitive; there is no fallback."""


class EndpointLeaseError(ManagedExecutorError):
    """A descriptor is not the exact connected TCP endpoint being leased."""


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ENV_VALUE = 16_384
_MAX_LABEL = 512
_HOST_OWNER_ENV = "POK_MANAGED_PROCESS_OWNER"
_MANAGED_BOT_EXTRA_ENV_KEYS = frozenset(
    {"POK_ARENA_SESSION_ID", "POK_TRACE_DECISIONS"}
)
_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_OUTPUT_FILES = 4
_MAX_READONLY_INPUTS = 8
_RESOURCE_LIMITS = (
    ("RLIMIT_NOFILE", 64),
    ("RLIMIT_NPROC", 64),
    ("RLIMIT_AS", 4_294_967_296),
    ("RLIMIT_FSIZE", 16_777_216),
    ("RLIMIT_CORE", 0),
)

_BASE_CHILD_ENVIRONMENT = {
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONIOENCODING": "utf-8",
    "TMPDIR": "/tmp",
}

_NAMESPACE_FLAGS = (
    "--unshare-user",
    "--unshare-ipc",
    "--unshare-pid",
    "--unshare-net",
    "--unshare-uts",
    "--unshare-cgroup",
)

_BLOCKED_SYSCALLS = (
    "connect",
    "bind",
    "listen",
    "accept",
    "accept4",
    "unshare",
    "setns",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "move_mount",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    "open_tree",
    "mount_setattr",
    "bpf",
    "perf_event_open",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "userfaultfd",
    "fanotify_init",
    "open_by_handle_at",
    "name_to_handle_at",
    "add_key",
    "request_key",
    "keyctl",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "kexec_load",
    "kexec_file_load",
    "reboot",
    "swapon",
    "swapoff",
    "init_module",
    "finit_module",
    "delete_module",
    "seccomp",
    "_sysctl",
)

_ENOSYS_SYSCALLS = (
    # glibc retries clone(2) only when clone3(2) is unavailable. Returning
    # EPERM here disables ordinary pthreads and ProcessPoolExecutor, while
    # ENOSYS preserves the clone fallback whose namespace flags are filtered
    # separately below.
    "clone3",
)

# Ordinary clone remains available, but every namespace flag is denied.
_CLONE_NAMESPACE_FLAGS = (
    0x00020000,  # CLONE_NEWNS
    0x02000000,  # CLONE_NEWCGROUP
    0x04000000,  # CLONE_NEWUTS
    0x08000000,  # CLONE_NEWIPC
    0x10000000,  # CLONE_NEWUSER
    0x20000000,  # CLONE_NEWPID
    0x40000000,  # CLONE_NEWNET
)

_SECCOMP_POLICY_DOCUMENT = {
    "schema": "pok-managed-executor-seccomp-v1",
    "default": "allow",
    "socket": "errno(EPERM) when domain != AF_UNIX",
    "socketpair": "errno(EPERM) when domain != AF_UNIX",
    "clone": "errno(EPERM) when any namespace flag is present",
    "clone3": "errno(ENOSYS) so glibc falls back to filtered clone",
    "blocked_syscalls": list(_BLOCKED_SYSCALLS),
}
SECCOMP_POLICY_SHA256 = hashlib.sha256(
    json.dumps(
        _SECCOMP_POLICY_DOCUMENT,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def _literal_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    token = str(host).strip()
    if not token or "%" in token:
        raise EndpointLeaseError("endpoint host must be an unscoped literal IP address")
    try:
        address = ipaddress.ip_address(token)
    except ValueError as exc:
        raise EndpointLeaseError("endpoint host must be a literal IP address") from exc
    if not address.is_loopback:
        raise EndpointLeaseError("endpoint host must be loopback")
    return address


def _port(value: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EndpointLeaseError("endpoint port is invalid") from exc
    if not 1 <= port <= 65_535:
        raise EndpointLeaseError("endpoint port is invalid")
    return port


def _socket_peer(sock: socket.socket) -> tuple[str, int]:
    if sock.family not in {socket.AF_INET, socket.AF_INET6}:
        raise EndpointLeaseError("endpoint must use AF_INET or AF_INET6")
    try:
        socket_type = sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
    except OSError as exc:
        raise EndpointLeaseError("endpoint descriptor is unavailable") from exc
    if socket_type != socket.SOCK_STREAM:
        raise EndpointLeaseError("endpoint must be SOCK_STREAM")
    try:
        socket_error = int(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR))
    except OSError as exc:
        raise EndpointLeaseError("endpoint SO_ERROR cannot be inspected") from exc
    if socket_error != 0:
        raise EndpointLeaseError(
            f"endpoint has pending SO_ERROR: {os.strerror(socket_error)}"
        )
    try:
        peer = sock.getpeername()
    except OSError as exc:
        raise EndpointLeaseError("endpoint must already be connected") from exc
    if not isinstance(peer, tuple) or len(peer) < 2:
        raise EndpointLeaseError("endpoint peer shape is invalid")
    try:
        address = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
        port = int(peer[1])
    except (TypeError, ValueError, OverflowError) as exc:
        raise EndpointLeaseError("endpoint peer is invalid") from exc
    expected_version = 4 if sock.family == socket.AF_INET else 6
    if address.version != expected_version:
        raise EndpointLeaseError("endpoint family and peer address disagree")
    return str(address), port


class EndpointLease:
    """Own and revalidate one INET stream until one launch consumes it."""

    def __init__(
        self,
        sock: socket.socket,
        *,
        peer_host: str,
        peer_port: int,
    ) -> None:
        if not isinstance(sock, socket.socket):
            raise EndpointLeaseError("endpoint must be a socket.socket")
        address = _literal_address(peer_host)
        port = _port(peer_port)
        expected_family = socket.AF_INET if address.version == 4 else socket.AF_INET6
        if sock.family != expected_family:
            raise EndpointLeaseError("endpoint socket family does not match expected peer")
        observed_host, observed_port = _socket_peer(sock)
        if observed_host != str(address) or observed_port != port:
            raise EndpointLeaseError(
                "endpoint connected peer does not match the authorized peer"
            )
        if sock.fileno() < 3:
            raise EndpointLeaseError("endpoint descriptor must not replace stdio")
        os.set_inheritable(sock.fileno(), False)
        self._socket: socket.socket | None = sock
        self._peer_host = str(address)
        self._peer_port = port
        self._family = expected_family
        self._consumed = False
        self._lock = threading.Lock()

    @classmethod
    def adopt(
        cls,
        sock: socket.socket,
        *,
        peer_host: str,
        peer_port: int,
    ) -> "EndpointLease":
        return cls(sock, peer_host=peer_host, peer_port=peer_port)

    @classmethod
    def connect(
        cls,
        peer_host: str,
        peer_port: int,
        *,
        timeout: float = 5.0,
    ) -> "EndpointLease":
        address = _literal_address(peer_host)
        port = _port(peer_port)
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise EndpointLeaseError("endpoint timeout is invalid") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise EndpointLeaseError("endpoint timeout is invalid")
        family = socket.AF_INET if address.version == 4 else socket.AF_INET6
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout_value)
            destination = (
                (str(address), port)
                if family == socket.AF_INET
                else (str(address), port, 0, 0)
            )
            sock.connect(destination)
            sock.settimeout(None)
            return cls(sock, peer_host=str(address), peer_port=port)
        except BaseException:
            sock.close()
            raise

    @property
    def peer(self) -> tuple[str, int]:
        return self._peer_host, self._peer_port

    @property
    def family(self) -> socket.AddressFamily:
        return socket.AddressFamily(self._family)

    @property
    def consumed(self) -> bool:
        with self._lock:
            return self._consumed

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._socket is None or self._socket.fileno() < 0

    def _take_for_launch(self) -> int:
        with self._lock:
            if self._consumed:
                raise EndpointLeaseError("endpoint lease was already consumed")
            sock = self._socket
            if sock is None or sock.fileno() < 3:
                raise EndpointLeaseError("endpoint lease is closed")
            observed_host, observed_port = _socket_peer(sock)
            if (observed_host, observed_port) != self.peer:
                raise EndpointLeaseError("endpoint peer changed before launch")
            self._consumed = True
            return sock.fileno()

    def _close_after_launch(self) -> None:
        with self._lock:
            sock = self._socket
            self._socket = None
        if sock is not None:
            sock.close()

    def close(self) -> None:
        self._close_after_launch()

    def __enter__(self) -> "EndpointLease":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _ScmpArgCmp(ctypes.Structure):
    _fields_ = (
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    )


@dataclass
class _SeccompProgram:
    fd: int
    bpf_sha256: str
    policy_sha256: str
    size: int

    def close(self) -> None:
        if self.fd >= 0:
            descriptor, self.fd = self.fd, -1
            os.close(descriptor)


def _load_libseccomp() -> ctypes.CDLL:
    token = ctypes.util.find_library("seccomp")
    if not token:
        raise IsolationUnavailable("managed_executor_libseccomp_unavailable")
    try:
        library = ctypes.CDLL(token, use_errno=True)
    except OSError as exc:
        raise IsolationUnavailable("managed_executor_libseccomp_unloadable") from exc
    required = (
        "seccomp_init",
        "seccomp_release",
        "seccomp_syscall_resolve_name",
        "seccomp_rule_add_array",
        "seccomp_export_bpf",
    )
    if any(not hasattr(library, symbol) for symbol in required):
        raise IsolationUnavailable("managed_executor_libseccomp_api_incomplete")
    library.seccomp_init.argtypes = (ctypes.c_uint32,)
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = (ctypes.c_void_p,)
    library.seccomp_release.restype = None
    library.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add_array.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_ScmpArgCmp),
    )
    library.seccomp_rule_add_array.restype = ctypes.c_int
    library.seccomp_export_bpf.argtypes = (ctypes.c_void_p, ctypes.c_int)
    library.seccomp_export_bpf.restype = ctypes.c_int
    return library


def _seccomp_error(operation: str, result: int) -> IsolationUnavailable:
    code = -int(result) if int(result) < 0 else errno.EINVAL
    return IsolationUnavailable(
        f"managed_executor_seccomp_{operation}_failed: {os.strerror(code)}"
    )


def _compile_seccomp_program() -> _SeccompProgram:
    if not hasattr(os, "memfd_create"):
        raise IsolationUnavailable("managed_executor_memfd_unavailable")
    library = _load_libseccomp()
    scmp_act_allow = 0x7FFF0000
    scmp_act_errno_base = 0x00050000
    scmp_cmp_ne = 1
    scmp_cmp_masked_eq = 7
    context = library.seccomp_init(scmp_act_allow)
    if not context:
        raise IsolationUnavailable("managed_executor_seccomp_init_failed")

    def resolve(name: str, *, required: bool = False) -> int | None:
        number = int(library.seccomp_syscall_resolve_name(name.encode("ascii")))
        if number < 0:
            if required:
                raise IsolationUnavailable(
                    f"managed_executor_seccomp_syscall_unresolved: {name}"
                )
            return None
        return number

    def add_rule(
        number: int,
        comparisons: tuple[_ScmpArgCmp, ...] = (),
        *,
        error: int = errno.EPERM,
    ) -> None:
        if comparisons:
            array_type = _ScmpArgCmp * len(comparisons)
            array = array_type(*comparisons)
            pointer = ctypes.cast(array, ctypes.POINTER(_ScmpArgCmp))
        else:
            pointer = ctypes.POINTER(_ScmpArgCmp)()
        result = int(
            library.seccomp_rule_add_array(
                context,
                scmp_act_errno_base | int(error),
                number,
                len(comparisons),
                pointer,
            )
        )
        if result != 0:
            raise _seccomp_error("rule", result)

    descriptor = -1
    try:
        for name in ("socket", "socketpair"):
            number = resolve(name, required=True)
            assert number is not None
            add_rule(
                number,
                (
                    _ScmpArgCmp(
                        arg=0,
                        op=scmp_cmp_ne,
                        datum_a=socket.AF_UNIX,
                        datum_b=0,
                    ),
                ),
            )
        for name in _BLOCKED_SYSCALLS:
            number = resolve(name)
            if number is not None:
                add_rule(number)
        for name in _ENOSYS_SYSCALLS:
            number = resolve(name)
            if number is not None:
                add_rule(number, error=errno.ENOSYS)
        clone = resolve("clone")
        if clone is not None:
            for flag in _CLONE_NAMESPACE_FLAGS:
                add_rule(
                    clone,
                    (
                        _ScmpArgCmp(
                            arg=0,
                            op=scmp_cmp_masked_eq,
                            datum_a=flag,
                            datum_b=flag,
                        ),
                    ),
                )

        memfd_flags = int(getattr(os, "MFD_CLOEXEC", 0x0001)) | int(
            getattr(os, "MFD_ALLOW_SEALING", 0x0002)
        )
        descriptor = os.memfd_create(
            f"pok-seccomp-{SECCOMP_POLICY_SHA256[:16]}",
            flags=memfd_flags,
        )
        result = int(library.seccomp_export_bpf(context, descriptor))
        if result != 0:
            raise _seccomp_error("export", result)
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content:
            raise IsolationUnavailable("managed_executor_seccomp_bpf_empty")
        digest = hashlib.sha256(content).hexdigest()
        if not _HEX_64.fullmatch(digest):
            raise IsolationUnavailable("managed_executor_seccomp_bpf_digest_invalid")
        seals = (
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE
        )
        try:
            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        except OSError as exc:
            raise IsolationUnavailable(
                "managed_executor_seccomp_memfd_sealing_failed"
            ) from exc
        observed_seals = int(fcntl.fcntl(descriptor, fcntl.F_GET_SEALS))
        if observed_seals & seals != seals:
            raise IsolationUnavailable("managed_executor_seccomp_memfd_not_sealed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return _SeccompProgram(
            fd=descriptor,
            bpf_sha256=digest,
            policy_sha256=SECCOMP_POLICY_SHA256,
            size=len(content),
        )
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        library.seccomp_release(context)


@dataclass(frozen=True)
class ExecutorRuntime:
    bwrap: Path
    python: Path
    prlimit: Path
    runtime_mounts: tuple[Path, ...]

    @classmethod
    def discover(
        cls,
        *,
        bwrap: str | Path | None = None,
        python: str | Path | None = None,
        prlimit: str | Path | None = None,
    ) -> "ExecutorRuntime":
        bwrap_token = str(bwrap) if bwrap is not None else shutil.which("bwrap")
        python_token = str(python) if python is not None else "/usr/bin/python3"
        prlimit_token = str(prlimit) if prlimit is not None else "/usr/bin/prlimit"
        if not bwrap_token:
            raise IsolationUnavailable("managed_executor_bwrap_unavailable")
        bwrap_path = Path(bwrap_token).expanduser().resolve()
        python_path = Path(python_token).expanduser().resolve()
        prlimit_path = Path(prlimit_token).expanduser().resolve()
        for label, path in (
            ("bwrap", bwrap_path),
            ("python", python_path),
            ("prlimit", prlimit_path),
        ):
            if not path.is_file() or not os.access(path, os.X_OK):
                raise IsolationUnavailable(
                    f"managed_executor_{label}_not_executable: {path}"
                )
        try:
            python_path.relative_to(Path("/usr"))
        except ValueError as exc:
            raise IsolationUnavailable(
                "managed_executor_python_must_be_inside_usr_runtime"
            ) from exc
        mounts = tuple(
            Path(value)
            for value in ("/usr", "/lib", "/lib64")
            if Path(value).exists()
        )
        if not mounts or any(path == Path("/") for path in mounts):
            raise IsolationUnavailable("managed_executor_runtime_mount_contract_invalid")
        try:
            prlimit_path.relative_to(Path("/usr"))
        except ValueError as exc:
            raise IsolationUnavailable(
                "managed_executor_prlimit_must_be_inside_usr_runtime"
            ) from exc
        return cls(
            bwrap=bwrap_path,
            python=python_path,
            prlimit=prlimit_path,
            runtime_mounts=mounts,
        )


@dataclass(frozen=True)
class BotTiming:
    action_delay: float = 0.30
    hard_deadline: float = 55.0
    refinement_budget: float = 54.0
    baseline_target: float = 0.25

    def environment(self) -> dict[str, str]:
        values = {
            "action_delay": float(self.action_delay),
            "hard_deadline": float(self.hard_deadline),
            "refinement_budget": float(self.refinement_budget),
            "baseline_target": float(self.baseline_target),
        }
        if any(not math.isfinite(value) for value in values.values()):
            raise ManagedExecutorError("managed bot timing must be finite")
        if not 0.0 <= values["action_delay"] <= 59.0:
            raise ManagedExecutorError("managed bot action delay is unsafe")
        if not 0.05 <= values["hard_deadline"] <= 59.0:
            raise ManagedExecutorError("managed bot hard deadline is unsafe")
        if not 0.01 <= values["baseline_target"] <= values["hard_deadline"]:
            raise ManagedExecutorError("managed bot baseline target is unsafe")
        if not 0.04 <= values["refinement_budget"] <= values["hard_deadline"]:
            raise ManagedExecutorError("managed bot refinement budget is unsafe")
        return {
            "POK_OFFICIAL_ACTION_DELAY": str(values["action_delay"]),
            "POK_DECISION_HARD_DEADLINE_SEC": str(values["hard_deadline"]),
            "POK_DECISION_REFINEMENT_BUDGET_SEC": str(
                values["refinement_budget"]
            ),
            "POK_DECISION_BASELINE_TARGET_SEC": str(values["baseline_target"]),
            "POK_DECISION_BUDGET_SEC": str(values["hard_deadline"]),
        }


@dataclass(frozen=True)
class IsolationIdentity:
    policy_sha256: str
    bpf_sha256: str
    bpf_size: int
    namespaces: tuple[str, ...] = (
        "user",
        "ipc",
        "pid",
        "net",
        "uts",
        "cgroup",
    )
    nested_userns: str = "disabled-and-asserted"
    capabilities: str = "drop-all"
    environment: str = "clear-and-allowlist"
    host_process_environment: str = "optional-owner-marker-only"
    network: str = "isolated-netns-inherited-exact-peer-only"
    readonly_inputs: str = "named-ro-bind-only"
    writable_outputs: str = "named-new-file-bind-fd-only"
    resource_limits: tuple[tuple[str, int], ...] = (
        ("RLIMIT_NOFILE", 64),
        ("RLIMIT_NPROC", 64),
        ("RLIMIT_AS", 4_294_967_296),
        ("RLIMIT_FSIZE", 16_777_216),
        ("RLIMIT_CORE", 0),
    )


@dataclass(frozen=True)
class ManagedProcess:
    process: subprocess.Popen[bytes]
    isolation: IsolationIdentity


def _safe_text(value: object, label: str) -> str:
    token = str(value)
    if not token or len(token) > _MAX_LABEL or "\x00" in token:
        raise ManagedExecutorError(f"managed executor {label} is invalid")
    return token


def _validated_environment(
    additions: Mapping[str, object] | None,
) -> dict[str, str]:
    environment = dict(_BASE_CHILD_ENVIRONMENT)
    for raw_key, raw_value in (additions or {}).items():
        key = str(raw_key)
        value = str(raw_value)
        if not _ENV_NAME.fullmatch(key):
            raise ManagedExecutorError(f"managed executor environment key invalid: {key}")
        if key.startswith("LD_") or key in {
            PRECONNECTED_FD_ENV,
            PRECONNECTED_HOST_ENV,
            PRECONNECTED_PORT_ENV,
        }:
            raise ManagedExecutorError(
                f"managed executor reserved environment key: {key}"
            )
        if "\x00" in value or len(value) > _MAX_ENV_VALUE:
            raise ManagedExecutorError(
                f"managed executor environment value invalid: {key}"
            )
        environment[key] = value
    return environment


def _host_owner_environment(owner: str | None) -> dict[str, str]:
    if owner is None:
        return {}
    return {_HOST_OWNER_ENV: _safe_text(owner, "host process owner")}


def _artifact_root(path: str | Path, *, label: str) -> Path:
    root = Path(path).expanduser().resolve()
    if root == Path("/"):
        raise ManagedExecutorError(f"managed executor refuses to mount host / as {label}")
    try:
        metadata = root.stat()
    except OSError as exc:
        raise ManagedExecutorError(f"managed executor {label} is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ManagedExecutorError(f"managed executor {label} must be a directory")
    return root


def _entry_path(root: Path, relative: str | Path) -> PurePosixPath:
    token = str(relative).replace(os.sep, "/")
    parsed = PurePosixPath(token)
    if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts:
        raise ManagedExecutorError("managed bot entry path is invalid")
    host_entry = root.joinpath(*parsed.parts)
    try:
        metadata = host_entry.stat()
    except OSError as exc:
        raise ManagedExecutorError("managed bot entry is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ManagedExecutorError("managed bot entry must be a regular file")
    if host_entry.resolve().parent != root and root not in host_entry.resolve().parents:
        raise ManagedExecutorError("managed bot entry escapes artifact root")
    return parsed


def _base_bwrap_command(
    runtime: ExecutorRuntime,
    program: _SeccompProgram,
    environment: Mapping[str, str],
) -> list[str]:
    command = [
        str(runtime.bwrap),
        "--die-with-parent",
        "--new-session",
        *_NAMESPACE_FLAGS,
        "--disable-userns",
        "--assert-userns-disabled",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--uid",
        "65534",
        "--gid",
        "65534",
        "--hostname",
        "pok-managed",
    ]
    for key, value in sorted(environment.items()):
        command.extend(("--setenv", key, value))
    for mount in runtime.runtime_mounts:
        if mount == Path("/"):
            raise IsolationUnavailable("managed executor attempted host root mount")
        command.extend(("--ro-bind", str(mount), str(mount)))
    command.extend(
        (
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--size",
            "67108864",
            "--tmpfs",
            "/tmp",
            "--seccomp",
            str(program.fd),
        )
    )
    return command


def _open_decision_log(path: str | Path) -> tuple[Path, int]:
    destination = Path(path).expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as exc:
        raise ManagedExecutorError("managed decision log cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ManagedExecutorError("managed decision log must be one regular file")
        if metadata.st_uid != os.geteuid():
            raise ManagedExecutorError("managed decision log must be host-owned")
        os.fchmod(descriptor, 0o600)
        os.set_inheritable(descriptor, False)
        return destination, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_new_output_file(path: str | Path) -> tuple[Path, int]:
    destination = Path(path).expanduser().absolute()
    if destination == Path("/"):
        raise ManagedExecutorError("managed worker output path is invalid")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as exc:
        raise ManagedExecutorError(
            "managed worker output file must be a new safe path"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
        ):
            raise ManagedExecutorError(
                "managed worker output must be one host-owned regular file"
            )
        os.fchmod(descriptor, 0o600)
        os.set_inheritable(descriptor, False)
        return destination, descriptor
    except BaseException:
        os.close(descriptor)
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def _prepare_output_files(
    output_files: Mapping[str, str | Path] | None,
) -> list[tuple[str, Path, int]]:
    if not output_files:
        return []
    if len(output_files) > _MAX_OUTPUT_FILES:
        raise ManagedExecutorError("managed worker requested too many output files")
    prepared: list[tuple[str, Path, int]] = []
    try:
        for raw_name, raw_path in sorted(output_files.items()):
            name = str(raw_name)
            if not _OUTPUT_NAME.fullmatch(name) or name in {".", ".."}:
                raise ManagedExecutorError(
                    f"managed worker output name is invalid: {name}"
                )
            path, descriptor = _open_new_output_file(raw_path)
            prepared.append((name, path, descriptor))
        return prepared
    except BaseException:
        for _name, path, descriptor in prepared:
            os.close(descriptor)
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _prepare_readonly_inputs(
    work_root: Path,
    readonly_inputs: Mapping[str, str | Path] | None,
) -> list[tuple[str, Path]]:
    if not readonly_inputs:
        return []
    if len(readonly_inputs) > _MAX_READONLY_INPUTS:
        raise ManagedExecutorError("managed worker requested too many readonly inputs")
    prepared: list[tuple[str, Path]] = []
    for raw_name, raw_path in sorted(readonly_inputs.items()):
        name = str(raw_name)
        if not _OUTPUT_NAME.fullmatch(name) or name in {".", ".."}:
            raise ManagedExecutorError(
                f"managed worker readonly input name is invalid: {name}"
            )
        root = _artifact_root(raw_path, label=f"readonly input {name}")
        candidates = [work_root, *(item[1] for item in prepared)]
        if any(
            root == candidate
            or root in candidate.parents
            or candidate in root.parents
            for candidate in candidates
        ):
            raise ManagedExecutorError(
                "managed worker readonly roots must be mutually non-overlapping"
            )
        prepared.append((name, root))
    return prepared


def _resource_bounded_command(
    runtime: ExecutorRuntime,
    target: Sequence[str],
) -> list[str]:
    return [
        str(runtime.prlimit),
        "--nofile=64:64",
        "--nproc=64:64",
        "--as=4294967296:4294967296",
        "--fsize=16777216:16777216",
        "--core=0:0",
        *target,
    ]


def _spawn(
    command: Sequence[str],
    *,
    program: _SeccompProgram,
    inherited_fds: Sequence[int] = (),
    close_callbacks: Sequence[object] = (),
    stdin: int | IO[bytes] | None = subprocess.DEVNULL,
    stdout: int | IO[bytes] | None = subprocess.PIPE,
    stderr: int | IO[bytes] | None = subprocess.PIPE,
    start_new_session: bool = False,
    host_process_owner: str | None = None,
) -> ManagedProcess:
    pass_fds = tuple(dict.fromkeys((program.fd, *map(int, inherited_fds))))
    try:
        process = subprocess.Popen(
            tuple(command),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=_host_owner_environment(host_process_owner),
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=bool(start_new_session),
        )
        return ManagedProcess(
            process=process,
            isolation=IsolationIdentity(
                policy_sha256=program.policy_sha256,
                bpf_sha256=program.bpf_sha256,
                bpf_size=program.size,
            ),
        )
    finally:
        program.close()
        for callback in close_callbacks:
            callback()  # type: ignore[operator]


def launch_isolated_worker(
    work_root: str | Path,
    argv: Sequence[str],
    *,
    environment: Mapping[str, object] | None = None,
    readonly_inputs: Mapping[str, str | Path] | None = None,
    output_files: Mapping[str, str | Path] | None = None,
    runtime: ExecutorRuntime | None = None,
    stdin: int | IO[bytes] | None = subprocess.DEVNULL,
    stdout: int | IO[bytes] | None = subprocess.PIPE,
    stderr: int | IO[bytes] | None = subprocess.PIPE,
    start_new_session: bool = False,
    host_process_owner: str | None = None,
) -> ManagedProcess:
    """Launch a read-only ``/work`` worker using only the mounted /usr runtime."""

    _host_owner_environment(host_process_owner)
    root = _artifact_root(work_root, label="worker root")
    tokens = tuple(_safe_text(item, "worker argv") for item in argv)
    if not tokens:
        raise ManagedExecutorError("managed worker argv is empty")
    executable = PurePosixPath(tokens[0])
    if not executable.is_absolute() or not (
        str(executable).startswith("/usr/") or str(executable).startswith("/bin/")
    ):
        raise ManagedExecutorError("managed worker executable must use /usr runtime")
    child_environment = _validated_environment(environment)
    selected_runtime = runtime or ExecutorRuntime.discover()
    prepared_inputs = _prepare_readonly_inputs(root, readonly_inputs)
    prepared_outputs = _prepare_output_files(output_files)
    program: _SeccompProgram | None = None
    cleanup_transferred = False
    try:
        program = _compile_seccomp_program()
        command = _base_bwrap_command(selected_runtime, program, child_environment)
        command.extend(("--ro-bind", str(root), "/work"))
        if prepared_inputs:
            command.extend(("--dir", "/inputs"))
            for name, source in prepared_inputs:
                command.extend(
                    ("--ro-bind", str(source), f"/inputs/{name}")
                )
        output_descriptors: list[int] = []
        callbacks: list[object] = []
        if prepared_outputs:
            command.extend(("--dir", "/output"))
            for name, _path, descriptor in prepared_outputs:
                output_descriptors.append(descriptor)
                command.extend(
                    ("--bind-fd", str(descriptor), f"/output/{name}")
                )
                callbacks.append(lambda value=descriptor: os.close(value))
            command.extend(("--chmod", "0555", "/output"))
        command.extend(("--chdir", "/work"))
        command.extend(_resource_bounded_command(selected_runtime, tokens))
        cleanup_transferred = True
        return _spawn(
            command,
            program=program,
            inherited_fds=output_descriptors,
            close_callbacks=callbacks,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=start_new_session,
            host_process_owner=host_process_owner,
        )
    except BaseException:
        if not cleanup_transferred:
            if program is not None:
                program.close()
            for _name, path, descriptor in prepared_outputs:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                try:
                    path.unlink()
                except OSError:
                    pass
        raise


def launch_managed_bot(
    artifact_root: str | Path,
    endpoint: EndpointLease,
    *,
    entry_relative: str | Path = "national_bot.py",
    name: str,
    seat: str | None = None,
    decision_log: str | Path | None = None,
    seed: int | None = None,
    timing: BotTiming | None = None,
    environment: Mapping[str, object] | None = None,
    extra_args: Sequence[str] = (),
    runtime: ExecutorRuntime | None = None,
    stdin: int | IO[bytes] | None = subprocess.DEVNULL,
    stdout: int | IO[bytes] | None = subprocess.PIPE,
    stderr: int | IO[bytes] | None = subprocess.PIPE,
    start_new_session: bool = False,
    host_process_owner: str | None = None,
) -> ManagedProcess:
    _host_owner_environment(host_process_owner)
    if not isinstance(endpoint, EndpointLease):
        raise EndpointLeaseError("managed bot requires an EndpointLease")
    root = _artifact_root(artifact_root, label="bot artifact")
    entry = _entry_path(root, entry_relative)
    safe_name = _safe_text(name, "bot name")
    safe_seat = _safe_text(seat, "bot seat") if seat is not None else None
    safe_seed: int | None = None
    if seed is not None:
        try:
            safe_seed = int(seed)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ManagedExecutorError("managed bot seed is invalid") from exc
        if not 0 <= safe_seed <= 0x7FFFFFFFFFFFFFFF:
            raise ManagedExecutorError("managed bot seed is outside the safe range")

    child_environment = _validated_environment(None)
    child_environment.update((timing or BotTiming()).environment())
    for raw_key, raw_value in (environment or {}).items():
        key = str(raw_key)
        value = str(raw_value)
        if key not in _MANAGED_BOT_EXTRA_ENV_KEYS:
            raise ManagedExecutorError(
                f"managed bot environment key is not allowlisted: {key}"
            )
        if "\x00" in value or len(value) > _MAX_ENV_VALUE:
            raise ManagedExecutorError(
                f"managed bot environment value invalid: {key}"
            )
        child_environment[key] = value
    if safe_seed is not None:
        child_environment.update(
            {
                "POK_NATIVE_BOT_SEED": str(safe_seed),
                "PYTHONHASHSEED": str(safe_seed % 4_294_967_295),
            }
        )
    peer_host, peer_port = endpoint.peer
    selected_runtime = runtime or ExecutorRuntime.discover()
    program = _compile_seccomp_program()
    endpoint_fd = -1
    log_fd = -1
    callbacks: list[object] = []
    cleanup_transferred = False
    try:
        endpoint_fd = endpoint._take_for_launch()
        callbacks.append(endpoint._close_after_launch)
        child_environment.update(
            {
                PRECONNECTED_FD_ENV: str(endpoint_fd),
                PRECONNECTED_HOST_ENV: peer_host,
                PRECONNECTED_PORT_ENV: str(peer_port),
            }
        )
        command = _base_bwrap_command(selected_runtime, program, child_environment)
        command.extend(("--ro-bind", str(root), "/bot"))
        inherited_fds = [endpoint_fd]
        if decision_log is not None:
            _path, log_fd = _open_decision_log(decision_log)
            inherited_fds.append(log_fd)
            command.extend(
                (
                    "--dir",
                    "/evidence",
                    "--bind-fd",
                    str(log_fd),
                    "/evidence/decision.log",
                )
            )
            callbacks.append(lambda descriptor=log_fd: os.close(descriptor))
        target = [
                "--chdir",
                "/bot",
        ]
        command.extend(target)
        bot_command = [
                str(selected_runtime.python),
                "-I",
                "-B",
                "-c",
                SANDBOX_BOOTSTRAP,
                f"/bot/{entry.as_posix()}",
                "--host",
                peer_host,
                "--port",
                str(peer_port),
                "--name",
                safe_name,
        ]
        if safe_seat is not None:
            bot_command.extend(("--seat", safe_seat))
        if decision_log is not None:
            bot_command.extend(("--log", "/evidence/decision.log"))
        bot_command.extend(_safe_text(item, "bot argument") for item in extra_args)
        command.extend(_resource_bounded_command(selected_runtime, bot_command))
        cleanup_transferred = True
        return _spawn(
            command,
            program=program,
            inherited_fds=inherited_fds,
            close_callbacks=callbacks,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=start_new_session,
            host_process_owner=host_process_owner,
        )
    except BaseException:
        # _spawn owns cleanup after it is entered.  Before that point this block
        # closes every successfully acquired resource exactly once.
        if not cleanup_transferred:
            program.close()
            for callback in callbacks:
                callback()  # type: ignore[operator]
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise IsolationUnavailable(
            f"managed_executor_identity_file_unreadable: {path}"
        ) from exc
    return digest.hexdigest()


def _resolved_libseccomp_path(library: ctypes.CDLL) -> Path:
    candidates: list[Path] = []
    try:
        for line in Path("/proc/self/maps").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            fields = line.split()
            if not fields:
                continue
            token = fields[-1]
            if token.startswith("/") and "libseccomp.so" in Path(token).name:
                candidates.append(Path(token))
    except OSError:
        pass
    library_name = Path(str(getattr(library, "_name", ""))).name
    for directory in (Path("/lib"), Path("/usr/lib")):
        if library_name:
            candidates.extend(directory.glob(f"*/{library_name}"))
        candidates.extend(directory.glob("*/libseccomp.so.*"))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and "libseccomp.so" in resolved.name:
            return resolved
    raise IsolationUnavailable("managed_executor_libseccomp_path_unresolved")


def managed_executor_identity(
    runtime: ExecutorRuntime | None = None,
) -> dict[str, object]:
    """Content-bind tools and the same compiled BPF used by real launches."""

    selected_runtime = runtime or ExecutorRuntime.discover()
    library = _load_libseccomp()
    library_path = _resolved_libseccomp_path(library)
    program = _compile_seccomp_program()
    try:
        source_path = Path(__file__).resolve()
        bootstrap_sha256 = hashlib.sha256(
            SANDBOX_BOOTSTRAP.encode("utf-8")
        ).hexdigest()
        return {
            "schema": "pok-managed-executor-identity-v1",
            "source": {
                "path": "web/core/managed_bot_executor.py",
                "sha256": _sha256_file(source_path),
            },
            "sandbox_bootstrap_sha256": bootstrap_sha256,
            "seccomp": {
                "policy_sha256": program.policy_sha256,
                "bpf_sha256": program.bpf_sha256,
                "bpf_size": program.size,
                "library": {
                    "path": str(library_path),
                    "sha256": _sha256_file(library_path),
                },
            },
            "tools": {
                "bwrap": {
                    "path": str(selected_runtime.bwrap),
                    "sha256": _sha256_file(selected_runtime.bwrap),
                },
                "python": {
                    "path": str(selected_runtime.python),
                    "sha256": _sha256_file(selected_runtime.python),
                },
                "prlimit": {
                    "path": str(selected_runtime.prlimit),
                    "sha256": _sha256_file(selected_runtime.prlimit),
                },
            },
            "contract": {
                "namespaces": list(IsolationIdentity.namespaces),
                "explicit_namespace_flags": list(_NAMESPACE_FLAGS),
                "nested_userns": "disabled-and-asserted",
                "capabilities": "drop-all",
                "environment": "clear-and-allowlist",
                "host_process_environment": "optional-owner-marker-only",
                "network": "loopback-exact-peer-inherited-stream-only",
                "readonly_inputs": "named-ro-bind-only",
                "writable_outputs": "named-new-file-bind-fd-only",
                "max_output_files": _MAX_OUTPUT_FILES,
                "tmpfs_bytes": 67_108_864,
                "resource_limits": dict(_RESOURCE_LIMITS),
                "runtime_mounts": [
                    str(path) for path in selected_runtime.runtime_mounts
                ],
                "host_root_mounted": False,
            },
        }
    finally:
        program.close()


def _receive_line(sock: socket.socket, *, limit: int = 65_536) -> bytes:
    chunks = bytearray()
    while b"\n" not in chunks:
        chunk = sock.recv(min(4096, limit - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) >= limit:
            raise IsolationUnavailable("managed_executor_probe_report_too_large")
    return bytes(chunks).partition(b"\n")[0]


def probe_managed_executor(
    runtime: ExecutorRuntime | None = None,
    *,
    timeout: float = 8.0,
) -> dict[str, object]:
    selected_runtime = runtime or ExecutorRuntime.discover()
    identity: dict[str, object] | None = None
    process: subprocess.Popen[bytes] | None = None
    accepted: socket.socket | None = None
    lease: EndpointLease | None = None
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.settimeout(float(timeout))
    issues: list[str] = []
    observed: dict[str, object] = {}
    namespace_names = ("user", "ipc", "pid", "net", "uts", "cgroup")
    host_namespaces = {
        name: os.readlink(f"/proc/self/ns/{name}") for name in namespace_names
    }
    probe_source = r'''
import _socket
import argparse
import ctypes
import errno
import json
import os
import socket

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--port", required=True, type=int)
parser.add_argument("--name", required=True)
args = parser.parse_args()
wire = socket.create_connection((args.host, args.port), timeout=2.0)
status = {}
for line in open("/proc/self/status", encoding="utf-8"):
    key, separator, value = line.partition(":")
    if separator:
        status[key] = value.strip()
try:
    _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
except OSError as exc:
    inet_socket_errno = exc.errno
else:
    inet_socket_errno = 0
libc = ctypes.CDLL(None, use_errno=True)
class Sockaddr(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("data", ctypes.c_ubyte * 14)]
disconnect_address = Sockaddr()
disconnect_address.family = socket.AF_UNSPEC
ctypes.set_errno(0)
reconnect_result = libc.connect(
    wire.fileno(), ctypes.byref(disconnect_address), ctypes.sizeof(disconnect_address)
)
reconnect_errno = ctypes.get_errno()
ctypes.set_errno(0)
unshare_result = libc.unshare(0x10000000)
unshare_errno = ctypes.get_errno()
report = {
    "fd_inheritable": os.get_inheritable(wire.fileno()),
    "inet_socket_errno": inet_socket_errno,
    "namespaces": {
        name: os.readlink("/proc/self/ns/" + name)
        for name in ("user", "ipc", "pid", "net", "uts", "cgroup")
    },
    "reconnect_result": reconnect_result,
    "reconnect_errno": reconnect_errno,
    "status": {
        key: value for key, value in status.items()
        if key.startswith("Cap") or key in {"NoNewPrivs", "Seccomp", "Seccomp_filters"}
    },
    "unshare_result": unshare_result,
    "unshare_errno": unshare_errno,
}
wire.sendall((json.dumps(report, sort_keys=True) + "\n").encode("utf-8"))
wire.close()
'''
    try:
        identity = managed_executor_identity(selected_runtime)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        lease = EndpointLease.connect("127.0.0.1", port, timeout=timeout)
        accepted, _peer = listener.accept()
        accepted.settimeout(float(timeout))
        with tempfile.TemporaryDirectory(prefix="pok-managed-probe-") as temporary:
            artifact = Path(temporary) / "bot"
            artifact.mkdir(mode=0o700)
            (artifact / "national_bot.py").write_text(
                probe_source, encoding="utf-8"
            )
            launched = launch_managed_bot(
                artifact,
                lease,
                name="managed-executor-probe",
                runtime=selected_runtime,
            )
            process = launched.process
            payload = _receive_line(accepted)
            stdout, stderr = process.communicate(timeout=float(timeout))
            observed = json.loads(payload.decode("utf-8"))
            observed["returncode"] = process.returncode
            observed["stderr"] = stderr.decode("utf-8", errors="replace")[:1000]
            observed["stdout"] = stdout.decode("utf-8", errors="replace")[:1000]
            observed["lease_consumed"] = lease.consumed
            observed["lease_closed"] = lease.closed
            observed["launch_bpf_sha256"] = launched.isolation.bpf_sha256

        status = observed.get("status")
        if not isinstance(status, dict):
            issues.append("probe_status_missing")
            status = {}
        if status.get("NoNewPrivs") != "1":
            issues.append("probe_no_new_privs_missing")
        if status.get("Seccomp") != "2":
            issues.append("probe_seccomp_filter_missing")
        capability_values = [
            value for key, value in status.items() if str(key).startswith("Cap")
        ]
        if not capability_values or any(
            str(value) != "0000000000000000" for value in capability_values
        ):
            issues.append("probe_capabilities_not_empty")
        child_namespaces = observed.get("namespaces")
        if not isinstance(child_namespaces, dict) or any(
            child_namespaces.get(name) == host_namespaces[name]
            for name in namespace_names
        ):
            issues.append("probe_namespace_not_isolated")
        if observed.get("inet_socket_errno") != errno.EPERM:
            issues.append("probe_inet_socket_not_seccomp_blocked")
        if not (
            observed.get("reconnect_result") == -1
            and observed.get("reconnect_errno") == errno.EPERM
        ):
            issues.append("probe_inherited_stream_reconnect_not_blocked")
        if not (
            observed.get("unshare_result") == -1
            and observed.get("unshare_errno") == errno.EPERM
        ):
            issues.append("probe_nested_userns_not_blocked")
        if observed.get("fd_inheritable") is not False:
            issues.append("probe_endpoint_fd_remained_inheritable")
        if observed.get("returncode") != 0 or not payload:
            issues.append("probe_inherited_stream_failed")
        if not observed.get("lease_consumed") or not observed.get("lease_closed"):
            issues.append("probe_endpoint_lease_lifecycle_failed")
        seccomp_identity = identity.get("seccomp") if isinstance(identity, dict) else None
        if not isinstance(seccomp_identity, dict) or observed.get(
            "launch_bpf_sha256"
        ) != seccomp_identity.get("bpf_sha256"):
            issues.append("probe_bpf_identity_mismatch")
    except Exception as exc:
        issues.append(
            f"probe_exception:{type(exc).__name__}:{str(exc)[:300]}"
        )
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if lease is not None:
            lease.close()
        if accepted is not None:
            accepted.close()
        listener.close()
    return {
        "ok": not issues,
        "issues": issues,
        "identity": identity,
        "observed": observed,
        "host_namespaces": host_namespaces,
    }


__all__ = [
    "BotTiming",
    "EndpointLease",
    "EndpointLeaseError",
    "ExecutorRuntime",
    "IsolationIdentity",
    "IsolationUnavailable",
    "ManagedExecutorError",
    "ManagedProcess",
    "SECCOMP_POLICY_SHA256",
    "launch_isolated_worker",
    "launch_managed_bot",
    "managed_executor_identity",
    "probe_managed_executor",
]
