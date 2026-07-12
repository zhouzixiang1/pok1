"""Fail-closed artifact sealing and central execution for managed Arena bots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
from typing import Any, IO, Mapping
import uuid

from bot_artifact import artifact_manifest, canonical_digest, hash_path
from managed_bot_executor import (
    BotTiming,
    EndpointLease,
    ExecutorRuntime,
    IsolationUnavailable,
    ManagedProcess,
    launch_managed_bot,
    probe_managed_executor,
)
from national_protocol_quarantine import (
    ProtocolQuarantineError,
    quarantined_native_entry_sources,
)


ROOT = Path(__file__).resolve().parents[3]


class ArenaSandboxError(RuntimeError):
    """A managed bot cannot be sealed or launched with the required isolation."""


class ArenaSandboxUnavailable(ArenaSandboxError):
    """The host cannot provide the mandatory managed-bot sandbox."""


SandboxCapability = ExecutorRuntime


@dataclass(frozen=True)
class SealedBotArtifact:
    source: Path
    root: Path
    artifact_hash: str
    manifest_digest: str
    entry_relative: str = "national_bot.py"


def _reject_quarantined_native_entry(path: Path, *, label: str) -> None:
    try:
        matches = quarantined_native_entry_sources(path)
    except ProtocolQuarantineError as exc:
        raise ArenaSandboxError(
            "protocol_quarantine_authority_unavailable:"
            f"{label}:{type(exc).__name__}:{str(exc)[:180]}"
        ) from exc
    if matches:
        raise ArenaSandboxError(
            "protocol_quarantined_native_entry_forbidden:"
            f"{label}:matches={','.join(matches)}"
        )


def require_managed_sandbox(
    *,
    environment: Mapping[str, str] | None = None,
    probe: bool = True,
) -> SandboxCapability:
    """Resolve and exercise the repository-wide managed executor."""

    source = environment or os.environ
    try:
        requested_bwrap = str(source.get("POK_ARENA_BWRAP", "")).strip()
        capability = ExecutorRuntime.discover(
            bwrap=requested_bwrap or None,
            python=str(source.get("POK_ARENA_PYTHON", "/usr/bin/python3")),
            prlimit=str(source.get("POK_ARENA_PRLIMIT", "/usr/bin/prlimit")),
        )
        if probe:
            report = probe_managed_executor(capability)
            if report.get("ok") is not True:
                raise IsolationUnavailable(
                    "managed_executor_probe_failed:"
                    + ";".join(str(item) for item in (report.get("issues") or [])[:6])
                )
        return capability
    except IsolationUnavailable as exc:
        raise ArenaSandboxUnavailable(
            f"arena_sandbox_unavailable; managed execution has no fallback: {exc}"
        ) from exc


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
    _reject_quarantined_native_entry(source_path, label=source_path.name)
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


def launch_sandboxed_bot(
    artifact: SealedBotArtifact,
    capability: SandboxCapability,
    endpoint: EndpointLease,
    *,
    name: str,
    seat: str,
    session_id: str,
    action_delay: float,
    hard_deadline: float,
    refinement_budget: float,
    baseline_target: float,
    stdin: int | IO[bytes] | None = subprocess.DEVNULL,
    stdout: int | IO[bytes] | None = subprocess.PIPE,
    stderr: int | IO[bytes] | None = subprocess.PIPE,
    start_new_session: bool = True,
) -> ManagedProcess:
    """Launch an Arena bot through the repository-wide managed executor."""

    if seat not in {"upper", "lower"}:
        raise ArenaSandboxError("arena_managed_seat_invalid")
    _reject_quarantined_native_entry(artifact.root, label=name)
    if hash_path(artifact.root) != artifact.artifact_hash:
        raise ArenaSandboxError("arena_sealed_artifact_changed_before_launch")
    return launch_managed_bot(
        artifact.root,
        endpoint,
        entry_relative=artifact.entry_relative,
        name=name,
        seat=seat,
        timing=BotTiming(
            action_delay=action_delay,
            hard_deadline=hard_deadline,
            refinement_budget=refinement_budget,
            baseline_target=baseline_target,
        ),
        environment={"POK_ARENA_SESSION_ID": session_id},
        runtime=capability,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=start_new_session,
        host_process_owner=session_id,
    )


__all__ = [
    "ArenaSandboxError",
    "ArenaSandboxUnavailable",
    "SandboxCapability",
    "SealedBotArtifact",
    "launch_sandboxed_bot",
    "remove_sealed_artifacts",
    "require_managed_sandbox",
    "seal_bot_artifact",
]
