"""Fail-closed artifact sealing and Bubblewrap plans for managed Arena bots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any, Mapping
import uuid

from bot_artifact import artifact_manifest, canonical_digest, hash_path


ROOT = Path(__file__).resolve().parents[3]
SANDBOX_BOOTSTRAP = (
    "import runpy,sys;"
    "entry=sys.argv[1];"
    "sys.path.insert(0,'/bot');"
    "sys.argv=[entry]+sys.argv[2:];"
    "runpy.run_path(entry,run_name='__main__')"
)


class ArenaSandboxError(RuntimeError):
    """A managed bot cannot be sealed or launched with the required isolation."""


class ArenaSandboxUnavailable(ArenaSandboxError):
    """The host cannot provide the mandatory managed-bot sandbox."""


@dataclass(frozen=True)
class SandboxCapability:
    bwrap: Path
    python: Path
    python_root: Path | None


@dataclass(frozen=True)
class SealedBotArtifact:
    source: Path
    root: Path
    artifact_hash: str
    manifest_digest: str
    entry_relative: str = "national_bot.py"


@dataclass(frozen=True)
class SandboxedBotLaunch:
    command: tuple[str, ...]
    environment: dict[str, str]
    cwd: Path
    security_profile: str = "bwrap_ro_artifact_tmpfs_loopback_endpoint"


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _runtime_root(python: Path) -> Path | None:
    prefixes = {
        Path(sys.prefix).expanduser().resolve(),
        Path(sys.base_prefix).expanduser().resolve(),
    }
    for prefix in sorted(prefixes, key=lambda item: len(item.parts), reverse=True):
        if _path_is_within(python, prefix):
            return prefix
    return None


def _protected_runtime_root(path: Path) -> bool:
    protected = (
        ROOT,
        ROOT / "web" / "core" / "results",
        ROOT / "official_certificates",
    )
    return any(_path_is_within(path, item) for item in protected)


def _runtime_mount_arguments(capability: SandboxCapability) -> list[str]:
    arguments: list[str] = []
    mounted: list[Path] = []
    for raw in ("/usr", "/lib", "/lib64"):
        path = Path(raw)
        if path.exists():
            arguments.extend(("--ro-bind", raw, raw))
            mounted.append(path.resolve())

    python_root = capability.python_root
    if python_root is not None and not any(
        _path_is_within(capability.python, mount) for mount in mounted
    ):
        if _protected_runtime_root(python_root):
            raise ArenaSandboxUnavailable(
                "arena_sandbox_python_runtime_inside_protected_repository"
            )
        ancestors: list[Path] = []
        current = python_root.parent
        while current != current.parent:
            ancestors.append(current)
            current = current.parent
        for directory in reversed(ancestors):
            arguments.extend(("--dir", str(directory)))
        arguments.extend(("--ro-bind", str(python_root), str(python_root)))
    return arguments


def require_managed_sandbox(
    *,
    environment: Mapping[str, str] | None = None,
    probe: bool = True,
) -> SandboxCapability:
    """Resolve and, by default, exercise the mandatory Bubblewrap capability."""

    source = environment or os.environ
    requested_bwrap = str(source.get("POK_ARENA_BWRAP", "bwrap")).strip() or "bwrap"
    bwrap_token = shutil.which(requested_bwrap)
    if not bwrap_token:
        raise ArenaSandboxUnavailable(
            "arena_sandbox_bwrap_unavailable; managed execution has no fallback"
        )
    bwrap = Path(bwrap_token).expanduser().resolve()
    if not bwrap.is_file() or not os.access(bwrap, os.X_OK):
        raise ArenaSandboxUnavailable(
            f"arena_sandbox_bwrap_not_executable: {bwrap}"
        )

    python_token = str(source.get("POK_ARENA_PYTHON", sys.executable)).strip()
    python = Path(python_token).expanduser().resolve()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ArenaSandboxUnavailable(
            f"arena_sandbox_python_not_executable: {python}"
        )
    capability = SandboxCapability(
        bwrap=bwrap,
        python=python,
        python_root=_runtime_root(python),
    )
    mounts = _runtime_mount_arguments(capability)
    if probe:
        command = [
            str(bwrap),
            "--die-with-parent",
            "--unshare-all",
            "--share-net",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--clearenv",
            *mounts,
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            str(python),
            "-I",
            "-B",
            "-c",
            "pass",
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5.0,
                check=False,
                env={"PATH": os.defpath},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArenaSandboxUnavailable(
                f"arena_sandbox_probe_failed: {type(exc).__name__}: {str(exc)[:200]}"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().replace("\n", " ")[:240]
            raise ArenaSandboxUnavailable(
                "arena_sandbox_namespace_unavailable"
                + (f": {detail}" if detail else "")
            )
    return capability


def _copy_verified_file(source: Path, target: Path, manifest_row: dict[str, Any]) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(source, flags)
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ArenaSandboxError(f"arena_seal_source_not_regular: {source}")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(descriptor, "rb", closefd=False) as input_handle:
            with target.open("xb") as output_handle:
                while True:
                    chunk = input_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    output_handle.write(chunk)
                output_handle.flush()
                os.fsync(output_handle.fileno())
    finally:
        os.close(descriptor)
    if size != int(manifest_row.get("size", -1)) or digest.hexdigest() != str(
        manifest_row.get("sha256") or ""
    ):
        raise ArenaSandboxError(f"arena_seal_source_changed_while_copying: {source}")
    target.chmod(0o444)


def _remove_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        path.unlink()
        return
    for child in path.rglob("*"):
        if child.is_dir() and not child.is_symlink():
            child.chmod(0o700)
        elif not child.is_symlink():
            child.chmod(0o600)
    path.chmod(0o700)
    shutil.rmtree(path)


def seal_bot_artifact(
    source: str | Path,
    destination: str | Path,
    *,
    expected_hash: str,
) -> SealedBotArtifact:
    """Copy exactly the content-bound published subject into a read-only tree."""

    source_path = Path(source).expanduser().absolute()
    destination_path = Path(destination).expanduser().absolute()
    if len(expected_hash) != 64 or any(
        character not in "0123456789abcdef" for character in expected_hash.lower()
    ):
        raise ArenaSandboxError("arena_seal_expected_hash_invalid")
    source_manifest = artifact_manifest(source_path)
    if source_manifest.get("artifact_type") != "directory":
        raise ArenaSandboxError("arena_managed_bot_artifact_must_be_directory")
    if canonical_digest(source_manifest) != expected_hash:
        raise ArenaSandboxError("arena_seal_source_identity_mismatch")
    if destination_path.exists() or destination_path.is_symlink():
        raise ArenaSandboxError(f"arena_seal_destination_exists: {destination_path}")

    destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination_path.with_name(
        f"{destination_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    temporary.mkdir(mode=0o700)
    try:
        entries = source_manifest.get("entries") or []
        for item in entries:
            if not isinstance(item, dict) or item.get("path") == ".":
                continue
            relative = Path(str(item.get("path") or ""))
            if relative.is_absolute() or ".." in relative.parts:
                raise ArenaSandboxError("arena_seal_manifest_path_invalid")
            target = temporary / relative
            if item.get("type") == "directory":
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif item.get("type") == "file":
                _copy_verified_file(source_path / relative, target, item)
            else:
                raise ArenaSandboxError("arena_seal_manifest_entry_invalid")
        if not (temporary / "national_bot.py").is_file():
            raise ArenaSandboxError("arena_sealed_bot_missing_national_bot.py")
        if hash_path(temporary) != expected_hash:
            raise ArenaSandboxError("arena_seal_copy_identity_mismatch")
        if hash_path(source_path) != expected_hash:
            raise ArenaSandboxError("arena_seal_source_changed_during_copy")
        for directory in sorted(
            (item for item in temporary.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            directory.chmod(0o555)
        temporary.chmod(0o555)
        temporary.replace(destination_path)
    except BaseException:
        _remove_tree(temporary)
        raise
    return SealedBotArtifact(
        source=source_path,
        root=destination_path,
        artifact_hash=expected_hash,
        manifest_digest=canonical_digest(source_manifest),
    )


def remove_sealed_artifacts(path: str | Path) -> None:
    _remove_tree(Path(path).expanduser().absolute())


def build_sandboxed_bot_launch(
    artifact: SealedBotArtifact,
    capability: SandboxCapability,
    *,
    host: str,
    port: int,
    name: str,
    seat: str,
    session_id: str,
    action_delay: float,
    hard_deadline: float,
    refinement_budget: float,
    baseline_target: float,
) -> SandboxedBotLaunch:
    """Build a plan with no writable host bind and a loopback-only endpoint."""

    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError as exc:
        raise ArenaSandboxError("arena_managed_endpoint_is_not_an_ip") from exc
    if not loopback:
        raise ArenaSandboxError("arena_managed_endpoint_must_be_loopback")
    if not 1 <= int(port) <= 65_535:
        raise ArenaSandboxError("arena_managed_endpoint_port_invalid")
    if seat not in {"upper", "lower"}:
        raise ArenaSandboxError("arena_managed_seat_invalid")
    if hash_path(artifact.root) != artifact.artifact_hash:
        raise ArenaSandboxError("arena_sealed_artifact_changed_before_launch")

    timing_environment = {
        "PATH": f"{capability.python.parent}:/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "POK_ARENA_SESSION_ID": session_id,
        "POK_OFFICIAL_ACTION_DELAY": str(max(0.0, float(action_delay))),
        "POK_DECISION_HARD_DEADLINE_SEC": str(max(0.05, float(hard_deadline))),
        "POK_DECISION_REFINEMENT_BUDGET_SEC": str(
            max(0.04, float(refinement_budget))
        ),
        "POK_DECISION_BASELINE_TARGET_SEC": str(max(0.01, float(baseline_target))),
        "POK_DECISION_BUDGET_SEC": str(max(0.05, float(hard_deadline))),
    }
    command = [
        str(capability.bwrap),
        "--die-with-parent",
        "--unshare-all",
        "--share-net",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
    ]
    for key, value in timing_environment.items():
        command.extend(("--setenv", key, value))
    command.extend(_runtime_mount_arguments(capability))
    command.extend((
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        str(artifact.root),
        "/bot",
        "--chdir",
        "/bot",
        str(capability.python),
        "-I",
        "-B",
        "-c",
        SANDBOX_BOOTSTRAP,
        f"/bot/{artifact.entry_relative}",
        "--host",
        host,
        "--port",
        str(int(port)),
        "--name",
        name,
        "--seat",
        seat,
    ))
    return SandboxedBotLaunch(
        command=tuple(command),
        environment={
            "PATH": os.defpath,
            "POK_ARENA_SESSION_ID": session_id,
        },
        cwd=artifact.root,
    )
