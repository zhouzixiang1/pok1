"""Tracked execution identity for formal official-EXE certification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from bot_artifact import canonical_digest


ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = Path(__file__).with_name("official_execution_profile.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_execution_profile() -> dict[str, Any]:
    payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("official_execution_profile_invalid")
    return payload


def execution_profile_identity() -> dict[str, Any]:
    payload = load_execution_profile()
    return {
        "profile_id": str(payload.get("profile_id") or ""),
        "profile_sha256": _sha256(PROFILE_PATH),
        "profile_digest": canonical_digest(payload),
    }


def _bwrap_probe(command: str) -> str | None:
    probe = subprocess.run(
        [
            command,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--share-net",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "/usr/bin/true",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if probe.returncode == 0:
        return None
    detail = probe.stderr.decode("utf-8", errors="replace").strip()[:240]
    return f"official_sandbox_probe_failed: rc={probe.returncode} {detail}"


def validate_execution_profile(
    exe_path: str | Path,
    *,
    probe_sandbox: bool,
) -> dict[str, Any]:
    profile = load_execution_profile()
    identity = execution_profile_identity()
    issues: list[str] = []
    if profile.get("schema_version") != 1:
        issues.append("official_execution_profile_schema_mismatch")
    configured_exe = Path(exe_path).expanduser().resolve()
    expected_exe = ROOT / str((profile.get("official_exe") or {}).get("repository_path") or "")
    if configured_exe != expected_exe.resolve():
        issues.append("official_execution_profile_exe_path_mismatch")
    elif not configured_exe.is_file() or configured_exe.is_symlink():
        issues.append("official_execution_profile_exe_missing_or_symlink")
    elif _sha256(configured_exe) != str((profile.get("official_exe") or {}).get("sha256") or ""):
        issues.append("official_execution_profile_exe_sha256_mismatch")

    observed_tools: dict[str, Any] = {}
    tools = profile.get("tools") if isinstance(profile.get("tools"), dict) else {}
    for name, expected in tools.items():
        expected = expected if isinstance(expected, dict) else {}
        command = Path(str(expected.get("command_path") or ""))
        try:
            resolved = command.resolve(strict=True)
            observed_hash = _sha256(resolved)
        except Exception as exc:
            issues.append(f"official_execution_tool_unavailable:{name}:{type(exc).__name__}")
            continue
        observed_tools[str(name)] = {
            "command_path": str(command),
            "resolved_path": str(resolved),
            "sha256": observed_hash,
        }
        if str(resolved) != str(expected.get("resolved_path") or ""):
            issues.append(f"official_execution_tool_path_mismatch:{name}")
        if observed_hash != str(expected.get("sha256") or ""):
            issues.append(f"official_execution_tool_sha256_mismatch:{name}")
    if probe_sandbox and "bwrap" in observed_tools:
        probe_issue = _bwrap_probe(observed_tools["bwrap"]["command_path"])
        if probe_issue:
            issues.append(probe_issue)
    return {
        "ok": not issues,
        **identity,
        "issues": issues,
        "observed_exe": str(configured_exe),
        "observed_tools": observed_tools,
    }
