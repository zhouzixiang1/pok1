"""Tracked execution identity for formal official-EXE certification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
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


def validate_execution_profile(
    exe_path: str | Path,
    *,
    probe_sandbox: bool,
) -> dict[str, Any]:
    profile = load_execution_profile()
    identity = execution_profile_identity()
    issues: list[str] = []
    if profile.get("schema_version") != 3:
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
    observed_executor: dict[str, Any] = {}
    executor_probe: dict[str, Any] = {}
    if all(name in observed_tools for name in ("bwrap", "python", "prlimit")):
        try:
            from managed_bot_executor import (
                ExecutorRuntime,
                managed_executor_identity,
                probe_managed_executor,
            )

            runtime = ExecutorRuntime.discover(
                bwrap=observed_tools["bwrap"]["command_path"],
                python=observed_tools["python"]["command_path"],
                prlimit=observed_tools["prlimit"]["command_path"],
            )
            observed_executor = managed_executor_identity(runtime)
            if observed_executor != profile.get("managed_executor"):
                issues.append("official_managed_executor_identity_mismatch")
            if probe_sandbox:
                executor_probe = probe_managed_executor(runtime)
                if executor_probe.get("ok") is not True:
                    issues.append("official_managed_executor_probe_failed")
                    issues.extend(
                        f"official_managed_executor:{item}"
                        for item in (executor_probe.get("issues") or [])[:8]
                    )
        except Exception as exc:
            issues.append(
                "official_managed_executor_unavailable:"
                f"{type(exc).__name__}:{str(exc)[:180]}"
            )
    else:
        issues.append("official_managed_executor_tools_incomplete")
    return {
        "ok": not issues,
        **identity,
        "issues": issues,
        "observed_exe": str(configured_exe),
        "observed_tools": observed_tools,
        "observed_managed_executor": observed_executor,
        "managed_executor_probe": executor_probe,
    }
