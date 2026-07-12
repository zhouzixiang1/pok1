"""Seal and isolate native bots before formal official-EXE execution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from typing import Any, IO

from bot_artifact import artifact_manifest, canonical_digest, hash_path
from managed_bot_executor import (
    BotTiming,
    EndpointLease,
    ExecutorRuntime,
    ManagedProcess,
    launch_managed_bot,
)
from national_protocol_quarantine import (
    ProtocolQuarantineError,
    quarantined_native_entry_sources,
)
from official_execution_profile import load_execution_profile


@dataclass(frozen=True)
class SealedBotArtifact:
    source: Path
    root: Path
    entry_relative: str
    artifact_hash: str
    manifest_digest: str


@dataclass(frozen=True)
class OfficialBootstrapLaunchAuthorization:
    root_id: str
    artifact_hash: str
    selection_digest: str
    selection_json: str
    candidate_path: str


def _copy_verified_file(
    source: Path,
    target: Path,
    manifest_row: dict[str, Any],
) -> None:
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
            raise RuntimeError(f"official_seal_source_not_regular:{source}")
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
        raise RuntimeError(f"official_seal_source_changed_while_copying:{source}")
    target.chmod(0o444)


def _copy_manifest(source: Path, destination: Path, manifest: dict[str, Any]) -> str:
    artifact_type = manifest.get("artifact_type")
    entries = manifest.get("entries") if isinstance(manifest.get("entries"), list) else []
    if artifact_type == "directory":
        destination.mkdir(parents=True, mode=0o700)
        for item in entries:
            if not isinstance(item, dict) or item.get("path") == ".":
                continue
            relative = Path(str(item.get("path") or ""))
            target = destination / relative
            if item.get("type") == "directory":
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif item.get("type") == "file":
                _copy_verified_file(source / relative, target, item)
        entry_relative = "national_bot.py"
        if not (destination / entry_relative).is_file():
            raise FileNotFoundError("formal native bot is missing national_bot.py")
    elif artifact_type == "file":
        destination.mkdir(parents=True, mode=0o700)
        target = destination / source.name
        entries = manifest.get("entries") or []
        if len(entries) != 1 or not isinstance(entries[0], dict):
            raise ValueError("formal file artifact manifest is invalid")
        _copy_verified_file(source, target, entries[0])
        entry_relative = source.name
    else:
        raise ValueError("unsupported formal bot artifact type")
    for directory in sorted(
        (path for path in destination.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        directory.chmod(0o555)
    destination.chmod(0o555)
    return entry_relative


def seal_bot_artifact(
    source: str | Path,
    destination: str | Path,
    *,
    expected_hash: str,
) -> SealedBotArtifact:
    source_path = Path(os.path.abspath(os.fspath(Path(source).expanduser())))
    destination_path = Path(destination).expanduser().resolve()
    source_manifest = artifact_manifest(source_path)
    source_hash = canonical_digest(source_manifest)
    if source_hash != expected_hash:
        raise RuntimeError("official_seal_source_identity_mismatch")
    if destination_path.exists():
        shutil.rmtree(destination_path)
    temporary = destination_path.with_name(destination_path.name + ".tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    try:
        entry_relative = _copy_manifest(source_path, temporary, source_manifest)
    except BaseException:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        raise
    sealed_subject = temporary if source_path.is_dir() else temporary / source_path.name
    sealed_hash = hash_path(sealed_subject)
    if sealed_hash != expected_hash:
        shutil.rmtree(temporary)
        raise RuntimeError("official_seal_copy_identity_mismatch")
    if hash_path(source_path) != expected_hash:
        shutil.rmtree(temporary)
        raise RuntimeError("official_seal_source_changed_during_copy")
    temporary.replace(destination_path)
    return SealedBotArtifact(
        source=source_path,
        root=destination_path,
        entry_relative=entry_relative,
        artifact_hash=expected_hash,
        manifest_digest=canonical_digest(source_manifest),
    )


def launch_sandboxed_bot(
    artifact: SealedBotArtifact,
    endpoint: EndpointLease,
    *,
    name: str,
    seat: str | None,
    log_path: Path | None,
    supports_log: bool,
    extra_args: tuple[str, ...] = (),
    quarantine_authorization: OfficialBootstrapLaunchAuthorization | None = None,
    stdin: int | IO[bytes] | None = subprocess.DEVNULL,
    stdout: int | IO[bytes] | None = subprocess.PIPE,
    stderr: int | IO[bytes] | None = subprocess.PIPE,
    start_new_session: bool = False,
) -> ManagedProcess:
    """Launch a sealed formal bot through the one central isolation policy."""

    if hash_path(artifact.root) != artifact.artifact_hash:
        raise RuntimeError("official_sealed_artifact_changed_before_launch")
    try:
        quarantine_matches = quarantined_native_entry_sources(artifact.root)
    except ProtocolQuarantineError as exc:
        raise RuntimeError(
            "protocol_quarantine_authority_unavailable:official:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        ) from exc
    if quarantine_matches:
        expected_root = "national-v141-official-full-v5-signed-ledger-root"
        expected_v141_hash = (
            "67cd6059e17afdb333137a37a051764ccef405d03072addb92ebaa5a39631796"
        )
        selection: dict[str, Any] | None = None
        try:
            decoded = json.loads(
                quarantine_authorization.selection_json
                if quarantine_authorization is not None
                else "null"
            )
            selection = decoded if isinstance(decoded, dict) else None
        except Exception:
            selection = None
        valid_authorization = (
            quarantine_authorization is not None
            and quarantine_authorization.root_id == expected_root
            and quarantine_authorization.artifact_hash == expected_v141_hash
            and artifact.artifact_hash == expected_v141_hash
            and selection is not None
            and canonical_digest(selection)
            == quarantine_authorization.selection_digest
            and bool(quarantine_authorization.candidate_path)
        )
        if valid_authorization:
            try:
                from official_bootstrap import (
                    validate_signed_v5_ledger_bootstrap_selection,
                )

                validation = validate_signed_v5_ledger_bootstrap_selection(
                    selection,
                    expected_root,
                    quarantine_authorization.candidate_path,
                    allow_consumed=False,
                )
                valid_authorization = validation.get("valid") is True
            except Exception:
                valid_authorization = False
        if not valid_authorization:
            raise RuntimeError(
                "protocol_quarantined_native_entry_forbidden:official:"
                f"matches={','.join(quarantine_matches)}"
            )
    profile = load_execution_profile()
    tools = profile["tools"]
    runtime = ExecutorRuntime.discover(
        bwrap=str(tools["bwrap"]["command_path"]),
        python=str(tools["python"]["command_path"]),
        prlimit=str(
            (tools.get("prlimit") or {}).get("command_path")
            or "/usr/bin/prlimit"
        ),
    )
    sandbox = profile.get("sandbox") or {}
    timing = sandbox.get("bot_timing") or {}
    return launch_managed_bot(
        artifact.root,
        endpoint,
        entry_relative=artifact.entry_relative,
        name=name,
        seat=seat,
        decision_log=log_path if supports_log else None,
        timing=BotTiming(
            action_delay=float(timing.get("action_delay_sec", 0.30)),
            hard_deadline=float(timing.get("hard_deadline_sec", 55.0)),
            refinement_budget=float(timing.get("refinement_budget_sec", 54.0)),
            baseline_target=float(timing.get("baseline_target_sec", 0.25)),
        ),
        extra_args=extra_args,
        runtime=runtime,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=start_new_session,
    )


__all__ = [
    "OfficialBootstrapLaunchAuthorization",
    "SealedBotArtifact",
    "launch_sandboxed_bot",
    "seal_bot_artifact",
]
