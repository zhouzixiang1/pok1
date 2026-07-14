from __future__ import annotations

from pathlib import Path

from bots.research_native_lab.common_contracts.tools.verify_candidate_freeze import (
    CommandResult,
    FreezeState,
    evaluate_candidate_freeze,
)


RECORD_SHA256 = "ab" * 32
PROOF_SHA256 = "cd" * 32
BLOCK_HASH = "ef" * 32


class FakeAdapter:
    def __init__(self, *, info: CommandResult, verify: CommandResult, network="main", confirmations=6):
        self._responses = [info, verify]
        self.network = network
        self.confirmations = confirmations

    def ots(self, _arguments):
        return self._responses.pop(0)

    def bitcoin_rpc(self, method, _parameters):
        if method == "getblockchaininfo":
            return {"chain": self.network, "blocks": 800_005}
        if method == "getblockcount":
            return 800_005
        if method == "getblockhash":
            return BLOCK_HASH
        if method == "getblockheader":
            return {
                "hash": BLOCK_HASH,
                "height": 800_000,
                "time": 1_800_000_000,
                "confirmations": self.confirmations,
            }
        raise AssertionError(method)


def _evaluate(adapter: FakeAdapter):
    return evaluate_candidate_freeze(
        adapter,
        record_path=Path("candidate-freeze.json"),
        proof_path=Path("candidate-freeze.json.ots"),
        record_sha256=RECORD_SHA256,
        proof_sha256=PROOF_SHA256,
        bitcoin_rpc_url="http://127.0.0.1:8332",
        minimum_confirmations=6,
    )


def _valid_info(attestation: str) -> CommandResult:
    return CommandResult(
        0,
        f"File sha256 hash: {RECORD_SHA256}\nTimestamp:\nverify {attestation}\n",
    )


def _official_success() -> CommandResult:
    return CommandResult(
        0,
        "Success! Bitcoin block 800000 attests existence as of 2027-01-15 UTC\n",
    )


def test_pending_ots_attestation_is_not_a_formal_freeze() -> None:
    result = _evaluate(
        FakeAdapter(
            info=_valid_info("PendingAttestation('https://calendar.invalid')"),
            verify=CommandResult(1, ""),
        )
    )
    assert result["state"] == FreezeState.PENDING_BITCOIN
    assert result["reason"] == "awaiting_bitcoin_attestation"


def test_official_success_requires_mainnet_header_and_confirmations() -> None:
    result = _evaluate(
        FakeAdapter(
            info=_valid_info("BitcoinBlockHeaderAttestation(800000)"),
            verify=_official_success(),
        )
    )
    assert result["state"] == FreezeState.VERIFIED_BITCOIN
    assert result["bitcoin"] == {
        "network": "main",
        "height": 800_000,
        "block_hash": BLOCK_HASH,
        "attested_epoch": 1_800_000_000,
        "confirmations": 6,
        "best_height": 800_005,
    }


def test_regtest_or_too_few_confirmations_fail_closed() -> None:
    wrong_network = _evaluate(
        FakeAdapter(
            info=_valid_info("BitcoinBlockHeaderAttestation(800000)"),
            verify=_official_success(),
            network="regtest",
        )
    )
    assert wrong_network["state"] == FreezeState.VERIFIER_ERROR
    assert wrong_network["reason"] == "bitcoin_header_cross_check_mismatch"

    pending = _evaluate(
        FakeAdapter(
            info=_valid_info("BitcoinBlockHeaderAttestation(800000)"),
            verify=_official_success(),
            confirmations=5,
        )
    )
    assert pending["state"] == FreezeState.PENDING_BITCOIN
    assert pending["reason"] == "insufficient_confirmations"


def test_target_digest_or_bitcoin_merkle_mismatch_is_invalid() -> None:
    target = _evaluate(
        FakeAdapter(
            info=CommandResult(
                0,
                f"File sha256 hash: {'00' * 32}\nTimestamp:\n"
                "verify PendingAttestation('https://calendar.invalid')\n",
            ),
            verify=CommandResult(1, ""),
        )
    )
    assert target["state"] == FreezeState.INVALID
    assert target["reason"] == "target_digest_mismatch"

    merkle = _evaluate(
        FakeAdapter(
            info=_valid_info("BitcoinBlockHeaderAttestation(800000)"),
            verify=CommandResult(1, "Bitcoin verification failed: Digest does not match merkleroot\n"),
        )
    )
    assert merkle["state"] == FreezeState.INVALID
    assert merkle["reason"] == "bitcoin_merkle_mismatch"
