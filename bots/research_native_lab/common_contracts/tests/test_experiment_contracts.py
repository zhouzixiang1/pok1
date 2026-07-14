from __future__ import annotations

import json
import math

import pytest

from bots.research_native_lab.common_contracts.deadline import DecisionClock, SnapshotPublisher
from bots.research_native_lab.common_contracts.evaluation import (
    holm_adjust,
    paired_sign_flip_p_value,
    paired_difference_ci,
    required_paired_blocks_for_power,
)
from bots.research_native_lab.common_contracts.manifest import (
    build_tree_manifest,
    verify_tree_manifest,
)
from bots.research_native_lab.common_contracts.national_state import NationalGameState
from bots.research_native_lab.common_contracts.seeds import (
    SeedPartition,
    derive_child_seed,
)


def test_development_seed_splits_are_disjoint_and_contain_no_final_material() -> None:
    partition = SeedPartition.freeze(
        2026071203,
        {"train": 32, "dev": 8, "validation": 8},
    )
    partition.assert_disjoint()
    manifest = partition.public_manifest()
    assert "final-heldout" not in manifest["seeds"]
    assert manifest["scope"] == "development_only_no_final_heldout_material"
    partition.verify_manifest(manifest)
    assert derive_child_seed(partition.splits["train"][0], "deck", 0) == derive_child_seed(
        partition.splits["train"][0], "deck", 0
    )
    assert derive_child_seed(partition.splits["train"][0], "deck", 0) != derive_child_seed(
        partition.splits["train"][0], "policy", 0
    )


def test_paired_difference_and_holm_adjustment() -> None:
    estimate, low, high = paired_difference_ci(
        {f"b{i}": 0.75 for i in range(10)},
        {f"b{i}": 0.25 for i in range(10)},
        bootstrap_seed=55,
        bootstrap_samples=1000,
    )
    assert estimate == 0.5
    assert low < estimate < high
    adjusted = holm_adjust({"a": 0.01, "b": 0.03, "c": 0.5})
    assert adjusted == {"a": 0.03, "b": 0.06, "c": 0.5}
    assert paired_sign_flip_p_value([0.5] * 10, alternative="greater") > 0.0
    assert required_paired_blocks_for_power(
        target_difference=0.05,
        block_variance=0.25,
    ) >= 784


def test_anytime_snapshot_starts_with_legal_fallback_and_samples_mixture() -> None:
    state = NationalGameState.new_hand(1, small_blind=0)
    publisher = SnapshotPublisher(state, (("fold", 0.25), ("call", 0.75)))
    fallback = publisher.latest()
    assert fallback.iteration == 0 and fallback.source == "fallback"
    publisher.publish(10, (("call", 0.5), ("raise 200", 0.5)), source="search")
    wire, snapshot = publisher.sample(123)
    assert wire in {"call", "raise 200"}
    assert snapshot.iteration == 10
    with pytest.raises(ValueError):
        publisher.publish(11, (("check", 1.0),), source="illegal")
    with pytest.raises(ValueError, match="finite"):
        publisher.publish(11, (("call", math.nan),), source="nan")
    with pytest.raises(ValueError, match="overwritten"):
        publisher.publish(10, (("call", 1.0),), source="different")
    clock = DecisionClock.start(60.0)
    assert 53.0 < clock.remaining() <= 54.0
    assert clock.platform_action_timeout_sec == 60.0
    for invalid in (True, math.nan, math.inf, 0.0, -1.0):
        with pytest.raises(ValueError):
            DecisionClock.start(invalid)


def test_content_manifest_detects_asset_drift(tmp_path) -> None:
    asset = tmp_path / "asset.bin"
    asset.write_bytes(b"frozen-blueprint")
    bindings = {
        "entrypoint": "asset.bin",
        "config_digest": "01" * 32,
        "dependency_digest": "02" * 32,
        "resource_profile_digest": "03" * 32,
        "oracle_fixture_digest": "04" * 32,
        "action_set_digest": "05" * 32,
        "build_command": "python build.py --frozen",
        "run_command": "python main.py",
    }
    manifest = build_tree_manifest(tmp_path, bindings)
    verify_tree_manifest(tmp_path, manifest)
    asset.write_bytes(b"changed")
    with pytest.raises(ValueError):
        verify_tree_manifest(tmp_path, manifest)
    # Contract is JSON serializable for content-addressed reports.
    json.dumps(manifest, sort_keys=True)


def test_complete_tree_manifest_rejects_extra_mode_drift_and_symlink(tmp_path) -> None:
    entry = tmp_path / "main.py"
    entry.write_text("print('ok')\n", encoding="utf-8")
    bindings = {
        "entrypoint": "main.py",
        "config_digest": "01" * 32,
        "dependency_digest": "02" * 32,
        "resource_profile_digest": "03" * 32,
        "oracle_fixture_digest": "04" * 32,
        "action_set_digest": "05" * 32,
        "build_command": "python -m py_compile main.py",
        "run_command": "python main.py",
    }
    manifest = build_tree_manifest(tmp_path, bindings)
    (tmp_path / "undeclared.bin").write_bytes(b"strategy drift")
    with pytest.raises(ValueError, match="extra"):
        verify_tree_manifest(tmp_path, manifest)
    (tmp_path / "undeclared.bin").unlink()
    entry.chmod(0o755)
    with pytest.raises(ValueError, match="changed"):
        verify_tree_manifest(tmp_path, manifest)
    entry.chmod(0o644)
    (tmp_path / "link.py").symlink_to(entry)
    with pytest.raises(ValueError, match="regular non-symlink"):
        build_tree_manifest(tmp_path, bindings)
