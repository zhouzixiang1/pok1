"""Evaluation contract model for evolution git drift decisions.

This module is the single place that defines which repository paths can change
the meaning of an evolution evaluation. Runtime guards, checkpoint recovery and
publish reconciliation should depend on this contract instead of open-coding
HEAD-drift exceptions.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from bot_namespace import ACTIVE_BOT_PREFIX, bot_relpath, parse_bot_version
from evolution_scope import (
    CRITICAL_EXACT,
    CRITICAL_PREFIXES,
    NON_CONTRACT_PREFIXES,
    RUNTIME_PREFIXES,
    changed_paths_between_heads,
    normalize_repo_path,
)

CONTRACT_VERSION = 2
_BOT_NAME_RE = re.compile(rf"^{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)$")
_BOT_PATH_RE = re.compile(rf"^bots/{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)(?:/|$)")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def bot_version_from_name(value: Any) -> int | None:
    text = str(value or "").strip()
    match = _BOT_NAME_RE.match(text)
    if not match:
        return None
    return parse_bot_version(text)


def bot_version_from_path(path: str) -> int | None:
    match = _BOT_PATH_RE.match(normalize_repo_path(path))
    if not match:
        return None
    return _as_int(match.group("version"))


def _iter_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _iter_values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_values(item)
    else:
        yield value


def _extract_opponent_versions(checkpoint: dict[str, Any] | None) -> set[int]:
    versions: set[int] = set()
    if not isinstance(checkpoint, dict):
        return versions
    gate_results = checkpoint.get("gate_results")
    for value in _iter_values(gate_results if isinstance(gate_results, dict) else {}):
        version = bot_version_from_name(value)
        if version is not None:
            versions.add(version)
    return versions


def contract_bot_versions(
    *,
    candidate_v: int | None = None,
    source_v: int | None = None,
    checkpoint: dict[str, Any] | None = None,
    extra_versions: Iterable[int | str] | None = None,
) -> list[int]:
    """Return bot versions that are part of the active evaluation contract."""
    versions: set[int] = set()
    for value in (candidate_v, source_v):
        version = _as_int(value)
        if version is not None:
            versions.add(version)
    if isinstance(checkpoint, dict):
        for key in ("next_v", "source_v", "parent2_v"):
            version = _as_int(checkpoint.get(key))
            if version is not None:
                versions.add(version)
        versions.update(_extract_opponent_versions(checkpoint))
    for value in extra_versions or ():
        version = _as_int(value)
        if version is not None:
            versions.add(version)
    return sorted(versions)


def build_evaluation_contract(
    root: str | Path,
    *,
    candidate_v: int | None = None,
    source_v: int | None = None,
    checkpoint: dict[str, Any] | None = None,
    extra_versions: Iterable[int | str] | None = None,
    include_hash: bool = False,
) -> dict[str, Any]:
    """Build a serializable description of evaluation-sensitive paths."""
    bot_versions = contract_bot_versions(
        candidate_v=candidate_v,
        source_v=source_v,
        checkpoint=checkpoint,
        extra_versions=extra_versions,
    )
    prefixes = list(CRITICAL_PREFIXES) + [bot_relpath(version) + "/" for version in bot_versions]
    exact = sorted(CRITICAL_EXACT)
    contract = {
        "version": CONTRACT_VERSION,
        "path_prefixes": sorted(set(prefixes)),
        "path_exact": exact,
        "bot_versions": bot_versions,
        "runtime_prefixes": list(RUNTIME_PREFIXES),
        "non_contract_prefixes": list(NON_CONTRACT_PREFIXES),
    }
    if include_hash:
        contract["hash"] = evaluation_contract_hash(root, contract)
    return contract


def _is_runtime_path(path: str, runtime_prefixes: Iterable[str]) -> bool:
    path = normalize_repo_path(path)
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in runtime_prefixes)


def is_contract_path(path: str, contract: dict[str, Any]) -> bool:
    path = normalize_repo_path(path)
    if not path or _is_runtime_path(path, contract.get("runtime_prefixes") or RUNTIME_PREFIXES):
        return False
    non_contract_prefixes = contract.get("non_contract_prefixes") or NON_CONTRACT_PREFIXES
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in non_contract_prefixes):
        return False
    if path in set(contract.get("path_exact") or []):
        return True
    return any(path.startswith(prefix) for prefix in contract.get("path_prefixes") or [])


def classify_contract_paths(paths: Iterable[str], contract: dict[str, Any]) -> dict[str, Any]:
    contract_paths: list[str] = []
    external_paths: list[str] = []
    for raw in paths:
        path = normalize_repo_path(str(raw))
        if not path:
            continue
        if is_contract_path(path, contract):
            contract_paths.append(path)
        else:
            external_paths.append(path)
    return {
        "contract_paths": sorted(set(contract_paths)),
        "external_paths": sorted(set(external_paths)),
        "contract_count": len(set(contract_paths)),
        "external_count": len(set(external_paths)),
    }


def _git_ls_files(root: Path, pathspecs: list[str]) -> list[str]:
    if not pathspecs:
        return []
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=str(root),
        capture_output=True,
        text=False,
        timeout=30,
    )
    if proc.returncode != 0:
        return []
    return [
        normalize_repo_path(item.decode("utf-8", errors="replace"))
        for item in proc.stdout.split(b"\0")
        if item
    ]


def _iter_contract_files(root: Path, contract: dict[str, Any]) -> list[str]:
    pathspecs = list(contract.get("path_exact") or []) + list(contract.get("path_prefixes") or [])
    files = {
        rel for rel in _git_ls_files(root, pathspecs)
        if is_contract_path(rel, contract)
    }
    for prefix in contract.get("path_prefixes") or []:
        if not prefix.startswith(f"bots/{ACTIVE_BOT_PREFIX}"):
            continue
        base = root / prefix.rstrip("/")
        if not base.exists():
            continue
        for current_root, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                name for name in dirnames
                if name not in {"__pycache__", ".pytest_cache", ".mypy_cache"}
            ]
            for filename in filenames:
                if filename.endswith((".pyc", ".pyo")):
                    continue
                path = Path(current_root) / filename
                try:
                    rel = normalize_repo_path(str(path.relative_to(root)))
                except ValueError:
                    continue
                if is_contract_path(rel, contract):
                    files.add(rel)
    return sorted(files)


def evaluation_contract_hash(root: str | Path, contract: dict[str, Any]) -> str:
    """Hash the current on-disk content of contract files."""
    root_path = Path(root)
    digest = hashlib.sha256()
    digest.update(f"contract-v{contract.get('version', CONTRACT_VERSION)}\n".encode())
    for rel in _iter_contract_files(root_path, contract):
        path = root_path / rel
        if not path.is_file():
            continue
        digest.update(rel.encode("utf-8", errors="replace") + b"\0")
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            digest.update(f"ERROR:{type(exc).__name__}:{exc}".encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def evaluate_head_drift(
    root: str | Path,
    baseline_head: str,
    current_head: str,
    *,
    candidate_v: int | None = None,
    source_v: int | None = None,
    checkpoint: dict[str, Any] | None = None,
    extra_versions: Iterable[int | str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Return whether a HEAD change leaves the evaluation contract untouched."""
    if not baseline_head or not current_head or baseline_head == current_head:
        return False, {}
    changed_paths = changed_paths_between_heads(root, baseline_head, current_head)
    if changed_paths is None:
        return False, {
            "head_drift_paths_available": False,
            "evaluation_contract_unchanged": False,
        }
    contract = build_evaluation_contract(
        root,
        candidate_v=candidate_v,
        source_v=source_v,
        checkpoint=checkpoint,
        extra_versions=extra_versions,
        include_hash=False,
    )
    scope = classify_contract_paths(changed_paths, contract)
    allowed = not scope["contract_paths"]
    return allowed, {
        "head_drift_paths_available": True,
        "evaluation_contract_unchanged": allowed,
        "evaluation_contract": contract,
        "head_changed_paths": changed_paths[:80],
        "head_contract_paths": scope["contract_paths"][:40],
        "head_external_paths": scope["external_paths"][:40],
        # Compatibility fields for existing guard/recovery logs and tests.
        "head_blocking_entries": [f"?? {path}" for path in scope["contract_paths"][:40]],
        "head_ignored_entries": [f"?? {path}" for path in scope["external_paths"][:40]],
    }
