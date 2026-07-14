#!/usr/bin/env python3
"""Exact on-disk layout validation for a frozen v4 strength pool."""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:  # package import in tests; direct import when executed by the CLI
    from .v4_native_strength_artifacts import (
        ArtifactError,
        assert_read_only_tree,
        read_regular_bytes,
        tree_digest,
    )
except ImportError:  # pragma: no cover - script import path
    from v4_native_strength_artifacts import (
        ArtifactError,
        assert_read_only_tree,
        read_regular_bytes,
        tree_digest,
    )


def validate_frozen_output_tree(
    root: Path,
    *,
    final_root: Path,
    payload: dict[str, Any],
    raw_plan: bytes,
    plan_filename: str,
) -> None:
    def names(path: Path) -> set[str]:
        try:
            return {item.name for item in path.iterdir()}
        except OSError as exc:
            raise ArtifactError(
                f"cannot enumerate frozen output layout: {path}"
            ) from exc

    snapshots_root = root / "snapshots"
    candidate_root = snapshots_root / "candidate"
    opponents_root = snapshots_root / "opponents"
    candidate = payload["candidate_artifact"]
    opponent_labels = {row["label"] for row in payload["opponent_artifacts"]}
    if names(root) != {plan_filename, "snapshots"}:
        raise ArtifactError("frozen output root layout changed")
    if names(snapshots_root) != {"candidate", "opponents"}:
        raise ArtifactError("frozen snapshot layout changed")
    if names(candidate_root) != {candidate["label"]}:
        raise ArtifactError("frozen candidate layout changed")
    if names(opponents_root) != opponent_labels:
        raise ArtifactError("frozen opponent layout changed")
    if read_regular_bytes(root / plan_filename) != raw_plan:
        raise ArtifactError("raw strength plan differs from the sealed plan file")
    for artifact in [candidate, *payload["opponent_artifacts"]]:
        final_snapshot = Path(artifact["snapshot_path"])
        try:
            relative = final_snapshot.relative_to(final_root)
        except ValueError as exc:
            raise ArtifactError("snapshot escaped the frozen output root") from exc
        snapshot = root / relative
        if not (snapshot / artifact["native_entry"]).is_file():
            raise ArtifactError(f"snapshot native entry is missing: {snapshot}")
        if tree_digest(snapshot) != artifact["snapshot_directory_sha256"]:
            raise ArtifactError(f"snapshot digest changed: {snapshot}")
    assert_read_only_tree(root)


__all__ = ["validate_frozen_output_tree"]
