from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
from dataclasses import fields, replace
from pathlib import Path

import pytest

import bots.research_native_lab.common_contracts.seeds as seeds


CANDIDATE = "ab" * 32


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _formal_freeze_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    record = tmp_path / "candidate-freeze.json"
    proof = tmp_path / "candidate-freeze.json.ots"
    seeds.write_candidate_freeze_record(record, CANDIDATE)
    proof.write_bytes(b"offline-test-proof")
    record_digest = hashlib.sha256(record.read_bytes()).hexdigest()
    proof_digest = hashlib.sha256(proof.read_bytes()).hexdigest()
    tool_result = {
        "schema": "candidate-freeze-verification-result-v1",
        "state": "verified_bitcoin",
        "reason": "official_ots_and_bitcoin_core_verified",
        "record_sha256": record_digest,
        "proof_sha256": proof_digest,
        "minimum_confirmations": seeds.FREEZE_MINIMUM_CONFIRMATIONS,
        "bitcoin": {
            "network": "main",
            "height": 800_000,
            "block_hash": "cd" * 32,
            "attested_epoch": 1_800_000_000,
            "confirmations": seeds.FREEZE_MINIMUM_CONFIRMATIONS,
            "best_height": 800_005,
        },
    }

    def run(_command):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_canonical(tool_result), stderr=b""
        )

    monkeypatch.setattr(seeds, "_run_candidate_freeze_verifier", run)
    return seeds.verify_candidate_freeze(
        CANDIDATE,
        record,
        proof,
        wheelhouse=tmp_path / "wheelhouse",
        bitcoin_rpc_url="http://127.0.0.1:8332",
    )


def _formal_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return _formal_freeze_verification(tmp_path, monkeypatch).require_verified()


def _write_drand_evidence(root: Path, round_number: int) -> Path:
    root.mkdir()
    info = {
        "public_key": seeds.DRAND_PUBLIC_KEY,
        "period": seeds.DRAND_PERIOD_SEC,
        "genesis_time": seeds.DRAND_GENESIS_TIME,
        "genesis_seed": seeds.DRAND_GENESIS_SEED,
        "chain_hash": seeds.DRAND_CHAIN_HASH,
        "scheme": seeds.DRAND_SCHEME,
        "beacon_id": seeds.DRAND_BEACON_ID,
    }
    previous = {
        "round": round_number - 1,
        "signature": "11" * 96,
        "previous_signature": "22" * 96,
    }
    current = {
        "round": round_number,
        "signature": "33" * 96,
        "previous_signature": previous["signature"],
    }
    observations = []
    for index, endpoint in enumerate(seeds.DRAND_ENDPOINTS, start=1):
        info_name = f"relay-{index}-info.json"
        previous_name = f"relay-{index}-round-{round_number - 1}.json"
        current_name = f"relay-{index}-round-{round_number}.json"
        info_payload = _canonical(info)
        previous_payload = _canonical(previous)
        current_payload = _canonical(current)
        (root / info_name).write_bytes(info_payload)
        (root / previous_name).write_bytes(previous_payload)
        (root / current_name).write_bytes(current_payload)
        observations.append(
            {
                "endpoint": endpoint,
                "chain_info_file": info_name,
                "chain_info_sha256": hashlib.sha256(info_payload).hexdigest(),
                "beacon_file": current_name,
                "beacon_sha256": hashlib.sha256(current_payload).hexdigest(),
                "previous_beacon_file": previous_name,
                "previous_beacon_sha256": hashlib.sha256(previous_payload).hexdigest(),
            }
        )
    manifest = {
        "schema": "drand-cross-fetch-evidence-v1",
        "chain_hash": seeds.DRAND_CHAIN_HASH,
        "round": round_number,
        "observations": observations,
    }
    manifest_path = root / "evidence.json"
    manifest_path.write_bytes(_canonical(manifest))
    return manifest_path


def _rewrite_manifest_digest(manifest_path: Path, index: int, field: str, payload: bytes) -> None:
    manifest = json.loads(manifest_path.read_text())
    manifest["observations"][index][field] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))


def test_missing_proof_is_unstamped_and_cannot_create_a_plan(tmp_path: Path) -> None:
    record = tmp_path / "candidate-freeze.json"
    seeds.write_candidate_freeze_record(record, CANDIDATE)
    result = seeds.verify_candidate_freeze(
        CANDIDATE,
        record,
        tmp_path / "absent.ots",
        wheelhouse=tmp_path / "absent-wheelhouse",
        bitcoin_rpc_url="http://127.0.0.1:8332",
    )
    assert result.state is seeds.CandidateFreezeState.UNSTAMPED
    with pytest.raises(ValueError, match="not Bitcoin-verified"):
        result.require_verified()
    with pytest.raises(ValueError, match="typed candidate freeze"):
        seeds.FinalEvaluationPlan.after_candidate_freeze(CANDIDATE, 1_800_000_000)  # type: ignore[arg-type]


def test_only_formal_freeze_result_unlocks_future_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze = _formal_freeze(tmp_path, monkeypatch)
    plan = seeds.FinalEvaluationPlan.after_candidate_freeze(CANDIDATE, freeze)
    assert plan.freeze_attested_epoch == 1_800_000_000
    assert plan.beacon_not_before_epoch == 1_800_003_600
    assert plan.beacon_round == (
        (plan.beacon_not_before_epoch - seeds.DRAND_GENESIS_TIME + 29) // 30 + 1
    )
    fake = seeds.VerifiedCandidateFreeze(
        candidate_bundle_digest=CANDIDATE,
        record_sha256="01" * 32,
        proof_sha256="02" * 32,
        bitcoin_height=1,
        bitcoin_block_hash="03" * 32,
        attested_epoch=1_800_000_000,
        confirmations=6,
        receipt_digest="04" * 32,
    )
    with pytest.raises(ValueError, match="formal verifier"):
        seeds.FinalEvaluationPlan.after_candidate_freeze(CANDIDATE, fake)
    assert "bls_verified" not in inspect.signature(plan.verify_beacon).parameters


def test_saved_three_relay_payloads_then_official_verifier_unlock_seed_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze = _formal_freeze(tmp_path, monkeypatch)
    plan = seeds.FinalEvaluationPlan.after_candidate_freeze(CANDIDATE, freeze)
    manifest = _write_drand_evidence(tmp_path / "drand", plan.beacon_round)
    captured = {}

    def verify_official(*, lock, official_module_path, request):
        captured["lock"] = lock
        captured["module"] = official_module_path
        captured["request"] = request
        return {
            "schema": "drand-offline-verification-result-v1",
            "verified": True,
            "verified_rounds": [item["round"] for item in request["beacons"]],
        }

    monkeypatch.setattr(seeds, "_run_pinned_drand_verifier", verify_official)
    beacon = plan.verify_beacon(manifest, tmp_path / "unused-pinned-module.mjs")
    assert captured["request"]["chain_info"]["public_key"] == seeds.DRAND_PUBLIC_KEY
    assert captured["request"]["beacons"][-1]["previous_signature"] == "11" * 96
    stratum = "10" * 32
    identity = "01" * 32
    first = plan.derive_formal_deck_root_pool(beacon, stratum)
    assert first == plan.derive_formal_deck_root_pool(beacon, stratum)
    assert len(first) == seeds.FORMAL_DECK_ROOT_POOL_SIZE
    assert len(set(first)) == seeds.FORMAL_DECK_ROOT_POOL_SIZE
    assert all(0 <= value < 2**256 for value in first)
    assert any(value >= 2**63 for value in first)
    policy = plan.derive_formal_policy_seeds(beacon, stratum, identity, 512)
    assert first != policy
    assert len(policy) == 512
    assert all(0 <= value < 2**63 for value in policy)
    assert seeds.formal_deck_seed_namespace(stratum) == f"formal/{stratum}/deck-root"
    assert seeds.formal_policy_seed_namespace(stratum, identity) == (
        f"formal/{stratum}/policy/{identity}"
    )
    contract = json.loads(
        (Path(__file__).resolve().parents[1] / "contracts" / "final_randomness_v1.json").read_text()
    )
    vector = contract["derivation"]["golden_vector"]
    assert beacon.randomness == vector["beacon_randomness"]
    assert vector["candidate_bundle_digest"] == CANDIDATE
    assert vector["formal_seed_cohort_digest"] == stratum
    assert vector["artifact_identity_digest"] == identity
    assert {index: str(first[int(index)]) for index in ("0", "1", "8191")} == vector[
        "deck_root_uint256_decimal"
    ]
    assert {index: str(policy[int(index)]) for index in ("0", "1", "3")} == vector[
        "policy_seed_uint63_decimal"
    ]
    with pytest.raises(ValueError, match="registered formal seed stream"):
        plan.derive_seeds(beacon, "final/deck-block", 1)
    with pytest.raises(ValueError, match="8192-root pool"):
        plan.derive_seeds(beacon, seeds.formal_deck_seed_namespace(stratum), 512)
    with pytest.raises(ValueError, match="policy matrix"):
        plan.derive_formal_policy_seeds(
            beacon,
            stratum,
            identity,
            seeds.FORMAL_DECK_ROOT_POOL_SIZE + 1,
        )
    universe = ("01" * 32, "02" * 32, "03" * 32, "04" * 32)
    assert set(plan.rank_opponents(beacon, universe)) == set(universe)


def test_formal_beacon_recomputes_saved_payload_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze = _formal_freeze(tmp_path, monkeypatch)
    plan = seeds.FinalEvaluationPlan.after_candidate_freeze(CANDIDATE, freeze)
    manifest = _write_drand_evidence(tmp_path / "drand", plan.beacon_round)
    current = tmp_path / "drand" / f"relay-2-round-{plan.beacon_round}.json"
    current.write_bytes(current.read_bytes() + b" ")
    with pytest.raises(ValueError, match="saved drand beacon digest mismatch"):
        plan.verify_beacon(manifest, tmp_path / "unused.mjs")


def test_formal_beacon_rejects_relay_disagreement_and_bad_previous_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze = _formal_freeze(tmp_path, monkeypatch)
    plan = seeds.FinalEvaluationPlan.after_candidate_freeze(CANDIDATE, freeze)
    manifest = _write_drand_evidence(tmp_path / "disagree", plan.beacon_round)
    current = tmp_path / "disagree" / f"relay-3-round-{plan.beacon_round}.json"
    value = json.loads(current.read_text())
    value["signature"] = "44" * 96
    payload = _canonical(value)
    current.write_bytes(payload)
    _rewrite_manifest_digest(manifest, 2, "beacon_sha256", payload)
    with pytest.raises(ValueError, match="relays disagree"):
        plan.verify_beacon(manifest, tmp_path / "unused.mjs")

    manifest = _write_drand_evidence(tmp_path / "bad-link", plan.beacon_round)
    for index in range(1, 4):
        current = tmp_path / "bad-link" / f"relay-{index}-round-{plan.beacon_round}.json"
        value = json.loads(current.read_text())
        value["previous_signature"] = "55" * 96
        payload = _canonical(value)
        current.write_bytes(payload)
        _rewrite_manifest_digest(manifest, index - 1, "beacon_sha256", payload)
    with pytest.raises(ValueError, match="does not link"):
        plan.verify_beacon(manifest, tmp_path / "unused.mjs")


def test_fake_module_cannot_replace_hash_pinned_official_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze = _formal_freeze(tmp_path, monkeypatch)
    plan = seeds.FinalEvaluationPlan.after_candidate_freeze(CANDIDATE, freeze)
    manifest = _write_drand_evidence(tmp_path / "drand", plan.beacon_round)
    fake_module = tmp_path / "fake.mjs"
    fake_module.write_text(
        "export async function fetchBeacon() { return {verified: true}; }\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="official drand-client module digest mismatch"):
        plan.verify_beacon(manifest, fake_module)


def test_directly_constructed_plan_and_beacon_do_not_unlock_final_material() -> None:
    direct_verification = seeds.CandidateFreezeVerification(
        state=seeds.CandidateFreezeState.VERIFIED_BITCOIN,
        reason="forged",
        candidate_bundle_digest=CANDIDATE,
        record_sha256="01" * 32,
        proof_sha256="02" * 32,
        bitcoin_height=1,
        bitcoin_block_hash="03" * 32,
        attested_epoch=1_800_000_000,
        confirmations=6,
        receipt_digest="04" * 32,
    )
    with pytest.raises(ValueError, match="formal verifier"):
        direct_verification.require_verified()
    beacon_not_before = 1_800_003_600
    beacon_round = (
        (beacon_not_before - seeds.DRAND_GENESIS_TIME + 29) // 30 + 1
    )
    plan = seeds.FinalEvaluationPlan(
        candidate_bundle_digest=CANDIDATE,
        freeze_attested_epoch=1_800_000_000,
        freeze_receipt_digest="01" * 32,
        freeze_bitcoin_height=1,
        freeze_bitcoin_block_hash="02" * 32,
        beacon_not_before_epoch=beacon_not_before,
        beacon_round=beacon_round,
    )
    beacon = seeds.VerifiedBeacon(
        chain_hash=seeds.DRAND_CHAIN_HASH,
        round=beacon_round,
        randomness="03" * 32,
        signature="04" * 96,
        previous_signature=seeds.DRAND_GENESIS_SEED,
        receipt_digest="05" * 32,
    )
    with pytest.raises(ValueError, match="pinned diagnostic freeze path"):
        plan.derive_seeds(beacon, "final/deck-block", 1)


def test_dataclass_replace_strips_every_authority_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_types = (
        seeds.CandidateFreezeVerification,
        seeds.VerifiedCandidateFreeze,
        seeds.FinalEvaluationPlan,
        seeds.VerifiedBeacon,
    )
    for authority_type in authority_types:
        token_field = next(item for item in fields(authority_type) if item.name == "_token")
        assert token_field.init is False

    verification = _formal_freeze_verification(tmp_path, monkeypatch)
    copied_verification = replace(verification, reason="copied")
    with pytest.raises(ValueError, match="formal verifier"):
        copied_verification.require_verified()

    freeze = verification.require_verified()
    copied_freeze = replace(freeze, confirmations=freeze.confirmations + 1)
    with pytest.raises(ValueError, match="formal verifier"):
        seeds.FinalEvaluationPlan.after_candidate_freeze(CANDIDATE, copied_freeze)

    plan = seeds.FinalEvaluationPlan.after_candidate_freeze(CANDIDATE, freeze)
    copied_plan = replace(plan, freeze_receipt_digest="99" * 32)
    with pytest.raises(ValueError, match="pinned diagnostic freeze path"):
        copied_plan.verify_beacon(tmp_path / "absent.json", tmp_path / "absent.mjs")

    manifest = _write_drand_evidence(tmp_path / "drand", plan.beacon_round)

    def verify_official(*, request, **_kwargs):
        return {
            "schema": "drand-offline-verification-result-v1",
            "verified": True,
            "verified_rounds": [item["round"] for item in request["beacons"]],
        }

    monkeypatch.setattr(seeds, "_run_pinned_drand_verifier", verify_official)
    beacon = plan.verify_beacon(manifest, tmp_path / "unused.mjs")
    copied_beacon = replace(beacon, randomness="99" * 32)
    with pytest.raises(ValueError, match="formal BLS verification"):
        plan.derive_formal_deck_root_pool(copied_beacon, "10" * 32)
