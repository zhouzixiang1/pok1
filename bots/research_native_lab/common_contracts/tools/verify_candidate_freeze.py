"""Verify a candidate freeze against Bitcoin with OpenTimestamps 0.7.2.

The command is intentionally verify-only: it never stamps or upgrades a proof.
It builds an ephemeral environment from an exact offline wheelhouse, disables
the OTS cache and all remote calendar whitelists, asks the pinned official OTS
client to verify the target file, then cross-checks the attesting block and its
confirmations against the same loopback Bitcoin Core JSON-RPC endpoint.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence


TOOLS_DIR = Path(__file__).resolve().parent
LOCK_PATH = TOOLS_DIR / "verify_candidate_freeze.lock.json"
REQUIREMENTS_PATH = TOOLS_DIR / "verify_candidate_freeze.requirements.txt"
MAX_RECORD_BYTES = 1_048_576
MAX_PROOF_BYTES = 16_777_216
MAX_COMMAND_OUTPUT_BYTES = 262_144
SUCCESS_PATTERN = re.compile(r"Success! Bitcoin block ([0-9]+) attests existence as of ")
HEX_32_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class FreezeState:
    UNSTAMPED = "unstamped"
    PENDING_BITCOIN = "pending_bitcoin"
    VERIFIED_BITCOIN = "verified_bitcoin"
    INVALID = "invalid"
    VERIFIER_ERROR = "verifier_error"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _sha256_file(path: Path, maximum: int) -> str:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{path} must be a regular non-symlink file")
    if metadata.st_size > maximum:
        raise ValueError(f"{path} exceeds the frozen size bound")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(131_072):
            digest.update(chunk)
    return digest.hexdigest()


def _load_lock() -> dict[str, Any]:
    lock = json.loads(
        LOCK_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    if lock.get("schema") != "candidate-freeze-verifier-lock-v1":
        raise ValueError("unexpected candidate freeze verifier lock schema")
    if _sha256_file(Path(__file__).resolve(), MAX_RECORD_BYTES) != lock["adapter"]["sha256"]:
        raise ValueError("candidate freeze verifier adapter differs from its lock")
    if _sha256_file(REQUIREMENTS_PATH, MAX_RECORD_BYTES) != lock["requirements"]["sha256"]:
        raise ValueError("candidate freeze requirements differ from their lock")
    return lock


def _validate_loopback_rpc_url(value: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Bitcoin Core RPC URL must use http or https")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("formal freeze verification requires a loopback Bitcoin Core node")
    if parsed.query or parsed.fragment:
        raise ValueError("Bitcoin Core RPC URL must not contain query or fragment data")
    return parsed


def _validate_wheelhouse(wheelhouse: Path, lock: dict[str, Any]) -> None:
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ValueError("wheelhouse must be a non-symlink directory")
    expected = {row["filename"]: row["sha256"] for row in lock["wheels"]}
    actual = {entry.name for entry in wheelhouse.iterdir()}
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        raise ValueError(f"wheelhouse set mismatch; missing={missing}, extra={extra}")
    for filename, digest in expected.items():
        if _sha256_file(wheelhouse / filename, MAX_PROOF_BYTES) != digest:
            raise ValueError(f"wheel digest mismatch: {filename}")


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: str


class CandidateFreezeVerifierAdapter(Protocol):
    """Small injectable seam used by offline state-machine tests."""

    def ots(self, arguments: Sequence[str]) -> CommandResult: ...

    def bitcoin_rpc(self, method: str, parameters: Sequence[Any]) -> Any: ...


class HermeticOTSAdapter:
    def __init__(self, ots_executable: Path, bitcoin_rpc_url: str) -> None:
        self._ots_executable = ots_executable
        self._bitcoin_rpc_url = bitcoin_rpc_url
        self._parsed_rpc_url = _validate_loopback_rpc_url(bitcoin_rpc_url)

    def ots(self, arguments: Sequence[str]) -> CommandResult:
        completed = subprocess.run(
            [str(self._ots_executable), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            timeout=120,
            check=False,
            env={
                "HOME": os.devnull,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "PYTHONHASHSEED": "0",
            },
        )
        output = completed.stdout[:MAX_COMMAND_OUTPUT_BYTES].decode("utf-8", "replace")
        return CommandResult(completed.returncode, output)

    def bitcoin_rpc(self, method: str, parameters: Sequence[Any]) -> Any:
        parsed = self._parsed_rpc_url
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        authority = hostname
        if parsed.port is not None:
            authority += f":{parsed.port}"
        clean_url = urllib.parse.urlunsplit(
            (parsed.scheme, authority, parsed.path or "/", "", "")
        )
        headers = {"Content-Type": "application/json"}
        if parsed.username is not None:
            credentials = urllib.parse.unquote(parsed.username)
            credentials += ":" + urllib.parse.unquote(parsed.password or "")
            headers["Authorization"] = "Basic " + base64.b64encode(
                credentials.encode("utf-8")
            ).decode("ascii")
        request = urllib.request.Request(
            clean_url,
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "candidate-freeze-verifier",
                    "method": method,
                    "params": list(parameters),
                },
                separators=(",", ":"),
            ).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(MAX_COMMAND_OUTPUT_BYTES + 1)
        if len(payload) > MAX_COMMAND_OUTPUT_BYTES:
            raise ValueError("Bitcoin Core RPC response exceeds size bound")
        decoded = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
        if decoded.get("error") is not None or "result" not in decoded:
            raise ValueError("Bitcoin Core RPC returned an error")
        return decoded["result"]


def evaluate_candidate_freeze(
    adapter: CandidateFreezeVerifierAdapter,
    *,
    record_path: Path,
    proof_path: Path,
    record_sha256: str,
    proof_sha256: str,
    bitcoin_rpc_url: str,
    minimum_confirmations: int,
) -> dict[str, Any]:
    """Evaluate evidence without accepting a caller-provided verification flag."""

    base = {
        "schema": "candidate-freeze-verification-result-v1",
        "record_sha256": record_sha256,
        "proof_sha256": proof_sha256,
        "minimum_confirmations": minimum_confirmations,
    }
    info = adapter.ots(
        ("--no-cache", "--no-default-whitelist", "--no-bitcoin", "info", str(proof_path))
    )
    if info.returncode != 0:
        return {**base, "state": FreezeState.INVALID, "reason": "invalid_ots_container"}
    digest_line = f"File sha256 hash: {record_sha256}"
    if digest_line not in info.output.splitlines():
        return {**base, "state": FreezeState.INVALID, "reason": "target_digest_mismatch"}

    contains_pending = "PendingAttestation(" in info.output
    contains_bitcoin = "BitcoinBlockHeaderAttestation(" in info.output
    verified = adapter.ots(
        (
            "--no-cache",
            "--no-default-whitelist",
            "--bitcoin-node",
            bitcoin_rpc_url,
            "verify",
            "-f",
            str(record_path),
            str(proof_path),
        )
    )
    if verified.returncode != 0:
        if "File does not match original!" in verified.output:
            state, reason = FreezeState.INVALID, "target_digest_mismatch"
        elif "Bitcoin verification failed:" in verified.output:
            state, reason = FreezeState.INVALID, "bitcoin_merkle_mismatch"
        elif contains_bitcoin:
            state, reason = FreezeState.VERIFIER_ERROR, "bitcoin_verification_unavailable"
        elif contains_pending:
            state, reason = FreezeState.PENDING_BITCOIN, "awaiting_bitcoin_attestation"
        else:
            state, reason = FreezeState.INVALID, "no_supported_attestation"
        return {**base, "state": state, "reason": reason}

    heights = [int(value) for value in SUCCESS_PATTERN.findall(verified.output)]
    if len(heights) != 1:
        return {
            **base,
            "state": FreezeState.VERIFIER_ERROR,
            "reason": "ambiguous_official_success_receipt",
        }
    height = heights[0]
    try:
        chain_info = adapter.bitcoin_rpc("getblockchaininfo", ())
        best_height = adapter.bitcoin_rpc("getblockcount", ())
        block_hash = adapter.bitcoin_rpc("getblockhash", (height,))
        header = adapter.bitcoin_rpc("getblockheader", (block_hash, True))
    except Exception:
        return {
            **base,
            "state": FreezeState.VERIFIER_ERROR,
            "reason": "bitcoin_rpc_cross_check_failed",
        }

    valid_header = (
        isinstance(chain_info, dict)
        and chain_info.get("chain") == "main"
        and type(chain_info.get("blocks")) is int
        and chain_info["blocks"] == best_height
        and type(best_height) is int
        and type(block_hash) is str
        and HEX_32_PATTERN.fullmatch(block_hash) is not None
        and isinstance(header, dict)
        and header.get("hash") == block_hash
        and type(header.get("height")) is int
        and header["height"] == height
        and type(header.get("time")) is int
        and type(header.get("confirmations")) is int
        and header["confirmations"] > 0
        and best_height >= height
    )
    if not valid_header:
        return {
            **base,
            "state": FreezeState.VERIFIER_ERROR,
            "reason": "bitcoin_header_cross_check_mismatch",
        }
    confirmations = header["confirmations"]
    bitcoin = {
        "network": "main",
        "height": height,
        "block_hash": block_hash,
        "attested_epoch": header["time"],
        "confirmations": confirmations,
        "best_height": best_height,
    }
    if confirmations < minimum_confirmations:
        return {
            **base,
            "state": FreezeState.PENDING_BITCOIN,
            "reason": "insufficient_confirmations",
            "bitcoin": bitcoin,
        }
    return {
        **base,
        "state": FreezeState.VERIFIED_BITCOIN,
        "reason": "official_ots_and_bitcoin_core_verified",
        "bitcoin": bitcoin,
    }


def _build_hermetic_ots(wheelhouse: Path, lock: dict[str, Any], root: Path) -> Path:
    _validate_wheelhouse(wheelhouse, lock)
    environment = root / "venv"
    venv.EnvBuilder(with_pip=True, clear=True, symlinks=False).create(environment)
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    python = environment / bin_dir / ("python.exe" if os.name == "nt" else "python")
    pip = environment / bin_dir / ("pip.exe" if os.name == "nt" else "pip")
    ots = environment / bin_dir / ("ots.exe" if os.name == "nt" else "ots")
    completed = subprocess.run(
        [
            str(pip),
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--only-binary=:all:",
            "--require-hashes",
            "--find-links",
            str(wheelhouse),
            "-r",
            str(REQUIREMENTS_PATH),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=300,
        check=False,
        env={"HOME": os.devnull, "PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0"},
    )
    if completed.returncode != 0 or not python.is_file() or not ots.is_file():
        raise ValueError("offline installation of the locked OTS verifier failed")
    version = subprocess.run(
        [str(ots), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
        env={"HOME": os.devnull, "PATH": "/usr/bin:/bin"},
    )
    if version.returncode != 0 or version.stdout.strip() != lock["official_verifier"]["cli_version"]:
        raise ValueError("installed OTS client version differs from the lock")
    return ots


def verify(
    *,
    record_path: Path,
    proof_path: Path,
    wheelhouse: Path,
    bitcoin_rpc_url: str,
    minimum_confirmations: int,
) -> dict[str, Any]:
    if type(minimum_confirmations) is not int or minimum_confirmations < 1:
        raise ValueError("minimum confirmations must be a positive integer")
    _validate_loopback_rpc_url(bitcoin_rpc_url)
    lock = _load_lock()
    record_sha256 = _sha256_file(record_path, MAX_RECORD_BYTES)
    if not proof_path.exists():
        return {
            "schema": "candidate-freeze-verification-result-v1",
            "state": FreezeState.UNSTAMPED,
            "reason": "timestamp_file_missing",
            "record_sha256": record_sha256,
            "proof_sha256": None,
            "minimum_confirmations": minimum_confirmations,
        }
    proof_sha256 = _sha256_file(proof_path, MAX_PROOF_BYTES)
    with tempfile.TemporaryDirectory(prefix="candidate-freeze-verify-") as temporary:
        ots = _build_hermetic_ots(wheelhouse, lock, Path(temporary))
        adapter = HermeticOTSAdapter(ots, bitcoin_rpc_url)
        return evaluate_candidate_freeze(
            adapter,
            record_path=record_path,
            proof_path=proof_path,
            record_sha256=record_sha256,
            proof_sha256=proof_sha256,
            bitcoin_rpc_url=bitcoin_rpc_url,
            minimum_confirmations=minimum_confirmations,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--bitcoin-node", required=True)
    parser.add_argument("--minimum-confirmations", type=int, default=6)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = verify(
            record_path=args.record,
            proof_path=args.proof,
            wheelhouse=args.wheelhouse,
            bitcoin_rpc_url=args.bitcoin_node,
            minimum_confirmations=args.minimum_confirmations,
        )
    except Exception as error:
        result = {
            "schema": "candidate-freeze-verification-result-v1",
            "state": FreezeState.VERIFIER_ERROR,
            "reason": type(error).__name__,
        }
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
