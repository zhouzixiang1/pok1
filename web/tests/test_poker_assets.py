from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time

import pytest

import poker_assets
from poker_assets import (
    ARTIFACT_SIZE,
    CLASS_COUNT,
    MANIFEST_FILENAME,
    MAX_ARTIFACT_BYTES,
    MAX_MANIFEST_BYTES,
    RECORD_COUNT,
    AssetGenerationError,
    AssetIntegrityError,
    AssetSchemaError,
    PokerAssetError,
    build_system_hole_combo_metadata_asset,
    hole_combo_index,
    open_hole_combo_metadata_asset,
)


GENERATOR_COMMIT = "a" * 40


def _build(tmp_path, *, commit=GENERATOR_COMMIT):
    return build_system_hole_combo_metadata_asset(
        tmp_path / "system-assets",
        generator_commit=commit,
    )


def _rewrite_read_only(path: Path, content: bytes) -> None:
    os.chmod(path, 0o644)
    path.write_bytes(content)
    os.chmod(path, 0o444)


def test_real_asset_has_all_1326_unique_combinations_and_169_classes(tmp_path):
    receipt = _build(tmp_path)

    seen_pairs = set()
    class_counts = Counter()
    with open_hole_combo_metadata_asset(receipt.manifest_path.parent) as asset:
        assert len(asset) == RECORD_COUNT == 1326
        for expected_index, record in enumerate(asset.iter_records()):
            pair = (record.card_a, record.card_b)
            assert pair not in seen_pairs
            seen_pairs.add(pair)
            assert record.index == expected_index
            assert record.card_a < record.card_b
            assert hole_combo_index(*pair) == expected_index
            assert record.class_id == record.class_row * 13 + record.class_col
            assert record.class_combo_count in {4, 6, 12}
            class_counts[record.class_id] += 1

    assert len(seen_pairs) == 1326
    assert set(class_counts) == set(range(CLASS_COUNT))
    multiplicities = Counter(class_counts.values())
    assert multiplicities == {4: 78, 6: 13, 12: 78}


def test_class_ids_and_flags_are_invariant_under_suit_permutations(tmp_path):
    receipt = _build(tmp_path)

    with open_hole_combo_metadata_asset(receipt.manifest_path.parent) as asset:
        for high_rank in range(1, 13):
            for low_rank in range(high_rank):
                suited = {
                    asset.lookup(high_rank * 4 + suit, low_rank * 4 + suit).class_id
                    for suit in range(4)
                }
                offsuit = {
                    asset.lookup(high_rank * 4 + high_suit, low_rank * 4 + low_suit).class_id
                    for high_suit in range(4)
                    for low_suit in range(4)
                    if high_suit != low_suit
                }
                assert len(suited) == len(offsuit) == 1
                assert suited != offsuit
                suited_record = asset.lookup(high_rank * 4, low_rank * 4)
                offsuit_record = asset.lookup(high_rank * 4, low_rank * 4 + 1)
                assert suited_record.suited and not suited_record.pair
                assert not offsuit_record.suited and not offsuit_record.pair
                assert suited_record.class_combo_count == 4
                assert offsuit_record.class_combo_count == 12

        for rank in range(13):
            pair_class_ids = {
                asset.lookup(rank * 4 + suit_a, rank * 4 + suit_b).class_id
                for suit_a in range(4)
                for suit_b in range(suit_a + 1, 4)
            }
            assert pair_class_ids == {rank * 13 + rank}
            pair = asset.lookup(rank * 4, rank * 4 + 1)
            assert pair.pair and not pair.suited
            assert pair.class_combo_count == 6


def test_consumer_lookup_is_order_independent_o1_and_has_stable_labels(tmp_path):
    receipt = _build(tmp_path)

    with open_hole_combo_metadata_asset(receipt.manifest_path.parent) as asset:
        ace_king_suited = asset.lookup(48, 44)
        assert ace_king_suited == asset.lookup(44, 48)
        assert ace_king_suited.index == hole_combo_index(48, 44)
        assert ace_king_suited.class_label == "AKs"
        assert asset.lookup(48, 45).class_label == "AKo"
        assert asset.lookup(48, 49).class_label == "AA"

        with pytest.raises(ValueError, match="distinct"):
            asset.lookup(12, 12)
        with pytest.raises(ValueError, match=r"\[0, 51\]"):
            asset.lookup(-1, 12)
        with pytest.raises(ValueError, match="integer"):
            asset.lookup(True, 12)
        with pytest.raises(IndexError):
            asset.lookup_index(1326)

    with pytest.raises(PokerAssetError, match="closed"):
        asset.lookup(0, 1)


def test_manifest_header_and_sha256_contract_are_explicit(tmp_path):
    receipt = _build(tmp_path)
    manifest = json.loads(receipt.manifest_path.read_text(encoding="utf-8"))

    assert manifest["manifest_format"] == "pok-system-asset-manifest-v1"
    assert manifest["contract"]["schema_version"] == 1
    assert manifest["contract"]["binary_format_version"] == 1
    assert manifest["contract"]["generator"] == {
        "git_commit": GENERATOR_COMMIT,
        "module": "web/core/poker_assets.py",
    }
    assert manifest["contract"]["key_domain"]["pair_key"]["domain"].endswith("C(52,2)")
    assert "no equity" in manifest["contract"]["semantics"]["scope"]
    assert manifest["contract"]["consumer_contract"] == {
        "access": "read-only mmap with one fixed-width unpack per lookup",
        "api": "open_hole_combo_metadata_asset(...).lookup(card_a, card_b)",
        "auto_generate_on_read": False,
        "complexity": "O(1) pair-to-record index",
    }
    assert manifest["contract"]["ownership"]["owner"] == "evolution_system"
    assert "LLM workers are read-only" in manifest["contract"]["ownership"]["write_policy"]
    assert manifest["artifact"]["sha256"] == receipt.artifact_sha256
    assert manifest["contract"]["payload"]["sha256"] == receipt.payload_sha256
    assert manifest["contract_sha256"] == receipt.contract_sha256
    assert receipt.artifact_path.name.endswith(f"-{receipt.artifact_sha256}.bin")
    assert receipt.artifact_path.stat().st_mode & 0o222 == 0
    assert receipt.manifest_path.stat().st_mode & 0o222 == 0


def test_readonly_mmap_cold_start_and_size_are_bounded(tmp_path):
    receipt = _build(tmp_path)
    started = time.perf_counter()
    asset = open_hole_combo_metadata_asset(receipt.manifest_path.parent)
    elapsed = time.perf_counter() - started
    try:
        assert elapsed < 1.0
        assert receipt.artifact_path.stat().st_size == ARTIFACT_SIZE
        assert ARTIFACT_SIZE < MAX_ARTIFACT_BYTES
        assert receipt.manifest_path.stat().st_size < MAX_MANIFEST_BYTES
        assert asset.startup_stats.mapped_bytes == ARTIFACT_SIZE
        assert asset.startup_stats.eager_records_decoded == 0
        assert asset.startup_stats.storage == "readonly_mmap"
        assert asset.decoded_records == 0
        asset.lookup(0, 51)
        assert asset.decoded_records == 1
        with pytest.raises(TypeError):
            asset._mapping[0] = 0  # type: ignore[index]
    finally:
        asset.close()


def test_payload_or_manifest_corruption_is_rejected(tmp_path):
    receipt = _build(tmp_path)
    damaged = bytearray(receipt.artifact_path.read_bytes())
    damaged[-1] ^= 0xFF
    _rewrite_read_only(receipt.artifact_path, bytes(damaged))

    with pytest.raises(AssetIntegrityError, match="artifact SHA-256 mismatch"):
        open_hole_combo_metadata_asset(receipt.manifest_path.parent)

    other = _build(tmp_path / "other")
    manifest = json.loads(other.manifest_path.read_text(encoding="utf-8"))
    manifest["contract"]["semantics"]["scope"] = "pretend all-in equity table"
    _rewrite_read_only(
        other.manifest_path,
        poker_assets._canonical_json(manifest) + b"\n",
    )
    with pytest.raises(AssetSchemaError, match="frozen schema/semantics"):
        open_hole_combo_metadata_asset(other.manifest_path.parent)


def test_concurrent_builders_publish_one_valid_content_addressed_asset(tmp_path):
    asset_root = tmp_path / "shared"

    def build_once(_):
        return build_system_hole_combo_metadata_asset(
            asset_root,
            generator_commit=GENERATOR_COMMIT,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(build_once, range(24)))

    assert {item.artifact_sha256 for item in receipts} == {receipts[0].artifact_sha256}
    assert {item.contract_sha256 for item in receipts} == {receipts[0].contract_sha256}
    assert len(list(asset_root.glob("*.bin"))) == 1
    assert not list(asset_root.glob("*.tmp"))
    with open_hole_combo_metadata_asset(asset_root) as asset:
        assert asset.lookup(0, 51).index == 50


def test_manifest_publish_failure_preserves_previous_atomic_pointer(monkeypatch, tmp_path):
    asset_root = tmp_path / "atomic"
    first = build_system_hole_combo_metadata_asset(
        asset_root,
        generator_commit="1" * 40,
    )
    prior_manifest = first.manifest_path.read_bytes()

    def fail_publish(_root, _content):
        raise RuntimeError("injected manifest publish failure")

    monkeypatch.setattr(poker_assets, "_publish_manifest", fail_publish)
    with pytest.raises(RuntimeError, match="injected"):
        build_system_hole_combo_metadata_asset(
            asset_root,
            generator_commit="2" * 40,
        )

    assert (asset_root / MANIFEST_FILENAME).read_bytes() == prior_manifest
    assert not list(asset_root.glob("*.tmp"))
    with open_hole_combo_metadata_asset(asset_root) as asset:
        assert asset.generator_commit == "1" * 40
        assert asset.artifact_sha256 == first.artifact_sha256


def test_system_builder_rejects_candidate_owned_destination(monkeypatch, tmp_path):
    candidate_root = tmp_path / "bots"
    monkeypatch.setattr(poker_assets, "_repository_candidate_root", lambda: candidate_root)

    with pytest.raises(AssetGenerationError, match="cannot be generated inside bots"):
        build_system_hole_combo_metadata_asset(
            candidate_root / "national_v999" / "assets",
            generator_commit=GENERATOR_COMMIT,
        )
    assert not candidate_root.exists()


def test_consumer_never_auto_generates_or_writes_missing_asset(tmp_path):
    missing_root = tmp_path / "missing-system-assets"

    with pytest.raises(AssetIntegrityError, match="cannot open asset file"):
        open_hole_combo_metadata_asset(missing_root)

    assert not missing_root.exists()


@pytest.mark.parametrize("commit", ["", "g" * 40, "A" * 40, "a" * 39, "a" * 41])
def test_generator_commit_must_be_exact_lowercase_git_sha(tmp_path, commit):
    with pytest.raises(AssetSchemaError, match="generator_commit"):
        build_system_hole_combo_metadata_asset(
            tmp_path / "system-assets",
            generator_commit=commit,
        )
