from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


COMMON = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[4]


def _json(relative: str) -> dict:
    return json.loads((COMMON / relative).read_text(encoding="utf-8"))


def test_all_hash_bound_rule_inputs_match_frozen_base() -> None:
    manifest = _json("manifests/common_sources_v1.json")
    assert manifest["base_sha"] == "6ee160c93cee8d0afdad111c4c82bc6ddb6012ca"
    for entry in manifest["files"]:
        digest = hashlib.sha256((ROOT / entry["path"]).read_bytes()).hexdigest()
        assert digest == entry["sha256"], entry["path"]
    machine_oracles = {
        entry["path"]
        for entry in manifest["files"]
        if entry["authority"] == "official_exe_machine_readable_oracle"
    }
    assert machine_oracles == {
        "sever/tests/fixtures/official_raise_boundary_oracle_20260711.json",
        "sever/tests/fixtures/official_terminal_settlement_oracle_20260711.json",
    }


def test_formal_verifier_sources_licenses_and_local_files_are_pinned() -> None:
    manifest = _json("manifests/common_sources_v1.json")
    assert {row["distribution"] for row in manifest["external_verifiers"]} == {
        "drand-client@1.4.2",
        "opentimestamps-client==0.7.2",
    }
    for row in manifest["external_verifiers"]:
        assert row["license"]
        assert len(row["source_commit"]) == 40
    for row in manifest["formal_verifier_files"]:
        assert hashlib.sha256((ROOT / row["path"]).read_bytes()).hexdigest() == row[
            "sha256"
        ]


def test_frozen_current_pool_artifact_hashes_are_reproducible() -> None:
    sys.path.insert(0, str(ROOT / "web" / "core"))
    try:
        from bot_artifact import hash_path
    finally:
        sys.path.pop(0)
    snapshot = _json("contracts/current_pool_snapshot_20260712.json")
    assert snapshot["strength_use"] == (
        "visible_reference_and_post_freeze_heldout_universe_not_itself_blind"
    )
    assert len(snapshot["active_bots"]) == 11
    for row in snapshot["active_bots"]:
        assert hash_path(ROOT / "bots" / row["bot"]) == row["artifact_hash"]


def test_public_opponent_splits_are_disjoint_and_final_remains_committed() -> None:
    split = _json("contracts/opponent_splits_public_v1.json")
    names = []
    for namespace in ("train", "dev", "validation"):
        namespace_names = [row["bot"] for row in split[namespace]]
        assert len(namespace_names) == len(set(namespace_names))
        names.extend(namespace_names)
    assert len(names) == len(set(names))
    assert split["final_heldout"]["count"] == 4
    assert split["final_heldout"]["identity_unavailable_before_candidate_freeze"] is True
    assert split["final_heldout"]["derivation_contract"] == "contracts/final_randomness_v1.json"


def test_development_seed_manifest_contains_no_final_values() -> None:
    manifest = _json("contracts/development_seed_manifest_v1.json")
    assert "final-heldout" not in manifest["seeds"]
    assert manifest["scope"] == "development_only_no_final_heldout_material"
    final = _json("contracts/final_randomness_v1.json")
    assert final["security_claim"] == (
        "diagnostic derivation only; no current evidence proves entropy was "
        "unknown at the real freeze time"
    )
    assert final["formal_strength_available"] is False
    assert final["candidate_freeze"]["beacon_delay_sec"] == 3600
    proof = final["candidate_freeze"]["external_time_proof"]
    assert proof["accepted_state"] == "verified_bitcoin"
    assert proof["minimum_confirmations"] == 6
    assert proof["verifier"] == "opentimestamps-client==0.7.2"
    assert final["verification"]["verifier"] == "drand-client@1.4.2"
    assert final["verification"]["cross_fetch_minimum_independent_endpoints"] == 3
    assert final["derivation"]["deck_block_namespace"] == (
        "formal/<candidate_neutral_seed_cohort_digest>/deck-root"
    )
    assert final["derivation"]["policy_namespace"] == (
        "formal/<candidate_neutral_seed_cohort_digest>/policy/"
        "<artifact_identity_digest>"
    )
    assert final["derivation"]["required_root_count"] == 8192
    assert final["derivation"]["deck_typed_helper"].endswith(
        "derive_formal_deck_root_pool"
    )
    assert "uint256" in final["derivation"]["deck_output"]
    assert "0x7fffffffffffffff" in final["derivation"]["policy_output"]
    assert final["formal_status"].startswith("fail_closed_unavailable")
