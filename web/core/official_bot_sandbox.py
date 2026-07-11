"""Seal and isolate native bots before formal official-EXE execution."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
from typing import Any, Mapping

from bot_artifact import artifact_manifest, canonical_digest, hash_path
from official_execution_profile import load_execution_profile


SANDBOX_BOOTSTRAP = (
    "import runpy,sys;"
    "entry=sys.argv[1];"
    "sys.path.insert(0,'/bot');"
    "sys.argv=[entry]+sys.argv[2:];"
    "runpy.run_path(entry,run_name='__main__')"
)


@dataclass(frozen=True)
class SealedBotArtifact:
    source: Path
    root: Path
    entry_relative: str
    artifact_hash: str
    manifest_digest: str


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
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source / relative, target, follow_symlinks=False)
                target.chmod(0o444)
        entry_relative = "national_bot.py"
        if not (destination / entry_relative).is_file():
            raise FileNotFoundError("formal native bot is missing national_bot.py")
    elif artifact_type == "file":
        destination.mkdir(parents=True, mode=0o700)
        target = destination / source.name
        shutil.copyfile(source, target, follow_symlinks=False)
        target.chmod(0o444)
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
    source_path = Path(source).expanduser().resolve()
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
    entry_relative = _copy_manifest(source_path, temporary, source_manifest)
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


def build_sandboxed_bot_command(
    artifact: SealedBotArtifact,
    *,
    host: str,
    port: int,
    name: str,
    seat: str | None,
    log_path: Path | None,
    supports_log: bool,
    extra_args: tuple[str, ...] = (),
) -> tuple[list[str], dict[str, str]]:
    profile = load_execution_profile()
    tools = profile["tools"]
    bwrap = str(tools["bwrap"]["command_path"])
    python = str(tools["python"]["command_path"])
    command = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--share-net",
        "--cap-drop", "ALL",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "LC_ALL", "C.UTF-8",
        "--setenv", "PYTHONIOENCODING", "utf-8",
        "--setenv", "POK_OFFICIAL_ACTION_DELAY", "0.30",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--ro-bind", str(artifact.root), "/bot",
        "--dir", "/evidence",
    ]
    if log_path is not None and supports_log:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        log_path.chmod(0o600)
        command.extend(["--bind", str(log_path), "/evidence/decision.log"])
    command.extend([
        "--chdir", "/bot",
        python,
        "-I",
        "-B",
        "-c",
        SANDBOX_BOOTSTRAP,
        f"/bot/{artifact.entry_relative}",
        "--host", host,
        "--port", str(int(port)),
        "--name", name,
    ])
    if seat is not None:
        command.extend(["--seat", seat])
    if log_path is not None and supports_log:
        command.extend(["--log", "/evidence/decision.log"])
    command.extend(extra_args)
    environment = {
        "PATH": "/usr/bin:/bin",
        "POK_OFFICIAL_JOB_PROCESS_GROUP": os.environ.get(
            "POK_OFFICIAL_JOB_PROCESS_GROUP", ""
        ),
    }
    return command, environment
