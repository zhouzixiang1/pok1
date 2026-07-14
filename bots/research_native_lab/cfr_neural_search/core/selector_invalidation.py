"""Durable, no-clobber invalidation authority for selector workspaces.

The workspace marker is the immediate stop signal.  A second copy is recorded
under the route's source-controlled manifest tree so deleting the runtime
marker cannot make an invalidated run eligible again.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .identity import payload_sha256, require_sha256
from .strict_io import (
    atomic_json_create,
    exclusive_file_lock,
    load_hashed_json,
    read_regular_bytes,
    stable_tree_manifest,
    validate_real_directory,
)


INVALIDATION_SCHEMA = "route-b-invalidated-selector-run-v2"
INVALIDATION_NAME = "INVALIDATED.json"
INVALIDATION_AUTHORITY = "invalidated_never_freeze_or_publish"
REGISTRY_DIRECTORY = "manifests/invalidated_selector_runs"
REGISTRY_ENTRY_SCHEMA = "route-b-selector-invalidation-registry-entry-v1"
REGISTRY_SNAPSHOT_SCHEMA = "route-b-selector-invalidation-registry-snapshot-v1"
AUTHORITY_LOCK_NAME = ".m4-publication-invalidation.lock"
_AUTHORITY_RLOCK = threading.RLock()
_AUTHORITY_DEPTH = 0
_AUTHORITY_ROUTE: Path | None = None
_AUTHORITY_CONTEXT: Any = None


@contextmanager
def m4_authority_lock(route: str | Path):
    """Serialize invalidation, publication, rendering, writing, and verify."""

    global _AUTHORITY_CONTEXT, _AUTHORITY_DEPTH, _AUTHORITY_ROUTE
    route_path = validate_real_directory(route)
    runtime = validate_real_directory(route_path / "runtime_outputs")
    with _AUTHORITY_RLOCK:
        if _AUTHORITY_DEPTH == 0:
            context = exclusive_file_lock(
                runtime / AUTHORITY_LOCK_NAME,
                root=runtime,
            )
            context.__enter__()
            _AUTHORITY_CONTEXT = context
            _AUTHORITY_ROUTE = route_path
        elif _AUTHORITY_ROUTE != route_path:
            raise RuntimeError("nested M4 authority lock changed route root")
        _AUTHORITY_DEPTH += 1
        try:
            yield
        finally:
            _AUTHORITY_DEPTH -= 1
            if _AUTHORITY_DEPTH == 0:
                context = _AUTHORITY_CONTEXT
                _AUTHORITY_CONTEXT = None
                _AUTHORITY_ROUTE = None
                context.__exit__(None, None, None)


def _workspace_relative(route: Path, workspace: Path) -> str:
    route = validate_real_directory(route)
    runtime = validate_real_directory(route / "runtime_outputs")
    workspace = validate_real_directory(workspace)
    try:
        relative = workspace.relative_to(runtime)
    except ValueError as exc:
        raise ValueError("selector workspace must be below route runtime_outputs") from exc
    if relative == Path(".") or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("selector workspace must name one run below runtime_outputs")
    return relative.as_posix()


def workspace_identity(route: str | Path, workspace: str | Path) -> tuple[str, str]:
    """Return the canonical relative workspace name and its registry key."""

    relative = _workspace_relative(Path(route), Path(workspace))
    return relative, hashlib.sha256(relative.encode("utf-8")).hexdigest()


def registry_root_for_route(route: str | Path) -> Path:
    return validate_real_directory(Path(route)) / REGISTRY_DIRECTORY


def _validate_registry_entry(
    payload: Any,
    *,
    expected_file_stem: str,
) -> dict[str, Any]:
    expected_keys = {
        "schema",
        "authority",
        "workspace_relative",
        "workspace_identity_sha256",
        "marker_relative_path",
        "marker_payload",
        "marker_payload_sha256",
        "marker_file_sha256",
        "registered_at_utc",
    }
    if type(payload) is not dict or set(payload) != expected_keys:
        raise ValueError("selector invalidation registry entry differs from strict schema")
    relative = payload["workspace_relative"]
    if (
        payload["schema"] != REGISTRY_ENTRY_SCHEMA
        or payload["authority"] != INVALIDATION_AUTHORITY
        or type(relative) is not str
        or not relative
        or relative.startswith("/")
        or Path(relative).as_posix() != relative
        or any(part in {"", ".", ".."} for part in Path(relative).parts)
    ):
        raise ValueError("selector invalidation registry identity is invalid")
    identity = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    if (
        payload["workspace_identity_sha256"] != identity
        or expected_file_stem != identity
    ):
        raise ValueError("selector invalidation registry filename/identity mismatch")
    marker_relative = f"runtime_outputs/{relative}/{INVALIDATION_NAME}"
    if payload["marker_relative_path"] != marker_relative:
        raise ValueError("selector invalidation marker path is not canonical")
    marker = payload["marker_payload"]
    if (
        type(marker) is not dict
        or marker.get("schema") != INVALIDATION_SCHEMA
        or marker.get("authority") != INVALIDATION_AUTHORITY
    ):
        raise ValueError("selector invalidation registry marker payload is invalid")
    if payload_sha256(marker) != payload["marker_payload_sha256"]:
        raise ValueError("selector invalidation registry marker payload digest mismatch")
    checkpoint = marker.get("checkpoint")
    trace = marker.get("trace")
    if type(checkpoint) is not dict or type(trace) is not dict:
        raise ValueError("selector invalidation registry marker evidence is invalid")
    require_sha256(
        checkpoint.get("payload_sha256"),
        "invalidated selector checkpoint payload",
    )
    require_sha256(trace.get("payload_sha256"), "invalidated selector trace payload")
    require_sha256(payload["marker_file_sha256"], "selector invalidation marker file")
    if (
        type(payload["registered_at_utc"]) is not str
        or payload["registered_at_utc"] != marker.get("invalidated_at_utc")
    ):
        raise ValueError("selector invalidation registry timestamp mismatch")
    return payload


def invalidation_registry_snapshot(
    route: str | Path,
    *,
    registry_root: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and hash every permanent invalidation registry entry."""

    route_path = validate_real_directory(route)
    registry = validate_real_directory(
        registry_root_for_route(route_path) if registry_root is None else registry_root
    )
    manifest = stable_tree_manifest(
        registry,
        excluded_directory_names=frozenset(),
        excluded_suffixes=frozenset(),
    )
    entries: list[dict[str, Any]] = []
    for name, file_digest in sorted(manifest.items()):
        path = Path(name)
        if len(path.parts) != 1 or path.suffix != ".json" or len(path.stem) != 64:
            raise ValueError("selector invalidation registry contains an unknown entry")
        require_sha256(path.stem, "selector invalidation registry filename")
        payload = load_hashed_json(registry / name, root=registry)
        validated = _validate_registry_entry(payload, expected_file_stem=path.stem)
        entries.append(
            {
                "relative_path": name,
                "file_sha256": file_digest,
                "payload_sha256": payload_sha256(validated),
                "workspace_relative": validated["workspace_relative"],
                "workspace_identity_sha256": validated[
                    "workspace_identity_sha256"
                ],
                "marker_payload_sha256": validated["marker_payload_sha256"],
                "marker_file_sha256": validated["marker_file_sha256"],
                "selector_checkpoint_payload_sha256": validated["marker_payload"][
                    "checkpoint"
                ][
                    "payload_sha256"
                ],
                "selector_trace_payload_sha256": validated["marker_payload"]["trace"][
                    "payload_sha256"
                ],
            }
        )
    workspace_names = [entry["workspace_relative"] for entry in entries]
    if len(workspace_names) != len(set(workspace_names)):
        raise ValueError("selector invalidation registry repeats a workspace identity")
    if stable_tree_manifest(
        registry,
        excluded_directory_names=frozenset(),
        excluded_suffixes=frozenset(),
    ) != manifest:
        raise ValueError("selector invalidation registry changed during validation")
    manifest_payload = {"files": dict(sorted(manifest.items()))}
    return {
        "schema": REGISTRY_SNAPSHOT_SCHEMA,
        "relative_path": REGISTRY_DIRECTORY,
        "file_count": len(manifest),
        "files": manifest_payload["files"],
        "files_sha256": payload_sha256(manifest_payload),
        "entries": entries,
    }


def _create_or_require_identical(
    path: Path,
    payload: Mapping[str, Any],
    *,
    root: Path,
) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ValueError("no-clobber invalidation target is not a real regular file")
        if load_hashed_json(path, root=root) != dict(payload):
            raise ValueError("no-clobber invalidation target already has different facts")
        return
    try:
        atomic_json_create(path, payload, root=root)
    except ValueError:
        # A concurrent creator is acceptable only when it published the exact
        # same immutable facts.
        if path.is_symlink() or not path.is_file():
            raise
        if load_hashed_json(path, root=root) != dict(payload):
            raise
    if load_hashed_json(path, root=root) != dict(payload):
        raise RuntimeError("no-clobber invalidation publication failed readback")


def register_invalidation(
    route: str | Path,
    workspace: str | Path,
    marker_payload: Mapping[str, Any],
    *,
    registry_root: str | Path | None = None,
) -> dict[str, Any]:
    """No-clobber publish the permanent registry copy of one marker."""

    route_path = validate_real_directory(route)
    workspace_path = validate_real_directory(workspace)
    relative, identity = workspace_identity(route_path, workspace_path)
    marker_path = workspace_path / INVALIDATION_NAME
    marker_raw = read_regular_bytes(marker_path, root=workspace_path)
    loaded_marker = load_hashed_json(marker_path, root=workspace_path)
    if read_regular_bytes(marker_path, root=workspace_path) != marker_raw:
        raise ValueError("selector invalidation marker changed during registration")
    if loaded_marker != dict(marker_payload):
        raise ValueError("selector invalidation marker changed before registration")
    marker_sha = hashlib.sha256(marker_raw).hexdigest()
    registry = validate_real_directory(
        registry_root_for_route(route_path) if registry_root is None else registry_root
    )
    entry = {
        "schema": REGISTRY_ENTRY_SCHEMA,
        "authority": INVALIDATION_AUTHORITY,
        "workspace_relative": relative,
        "workspace_identity_sha256": identity,
        "marker_relative_path": f"runtime_outputs/{relative}/{INVALIDATION_NAME}",
        "marker_payload": dict(marker_payload),
        "marker_payload_sha256": payload_sha256(marker_payload),
        "marker_file_sha256": marker_sha,
        "registered_at_utc": marker_payload.get("invalidated_at_utc"),
    }
    _validate_registry_entry(entry, expected_file_stem=identity)
    target = registry / f"{identity}.json"
    _create_or_require_identical(target, entry, root=registry)
    # Validate the complete registry rather than trusting only the new entry.
    invalidation_registry_snapshot(route_path, registry_root=registry)
    return entry


def assert_workspace_not_invalidated(
    route: str | Path,
    workspace: str | Path,
    *,
    registry_root: str | Path | None = None,
) -> dict[str, Any]:
    """Fail closed when either invalidation authority names the workspace."""

    route_path = validate_real_directory(route)
    workspace_path = validate_real_directory(workspace)
    relative, identity = workspace_identity(route_path, workspace_path)
    snapshot = invalidation_registry_snapshot(route_path, registry_root=registry_root)
    marker = workspace_path / INVALIDATION_NAME
    marker_present = marker.exists() or marker.is_symlink()
    registry_present = f"{identity}.json" in snapshot["files"]
    if marker_present or registry_present:
        authorities = []
        if marker_present:
            authorities.append("workspace marker")
        if registry_present:
            authorities.append("permanent registry")
        raise ValueError(
            f"selector workspace {relative!r} is invalidated by "
            + " and ".join(authorities)
        )
    return snapshot
