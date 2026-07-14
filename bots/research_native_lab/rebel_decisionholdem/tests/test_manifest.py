from __future__ import annotations

from ..tools import milestone_manifest


def test_committed_milestone_manifest_is_complete_and_self_consistent() -> None:
    assert milestone_manifest.verify_manifest() == []


def test_common_dependency_drift_invalidates_milestone_manifest(
    monkeypatch,
) -> None:
    original = milestone_manifest.build_common_interface_snapshot

    def drifted_snapshot() -> dict[str, object]:
        return original() | {"package_tree_sha256": "0" * 64}

    monkeypatch.setattr(
        milestone_manifest,
        "build_common_interface_snapshot",
        drifted_snapshot,
    )
    assert (
        "common_interface snapshot differs from current package"
        in milestone_manifest.verify_manifest()
    )
