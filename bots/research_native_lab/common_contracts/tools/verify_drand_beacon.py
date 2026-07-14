"""Provision and cross-fetch evidence for the frozen drand verifier.

This tool never decides whether a beacon is valid.  Formal acceptance lives in
``seeds.FinalEvaluationPlan.verify_beacon``: it re-reads the files written here,
recomputes their hashes, checks all three relays, and invokes the pinned
official BLS verifier.  Keeping fetching separate prevents an HTTP response or
caller-supplied boolean from becoming a verification receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any


TOOLS_DIR = Path(__file__).resolve().parent
LOCK_PATH = TOOLS_DIR / "verify_drand_beacon.lock.json"
MAX_DOWNLOAD_BYTES = 2_000_000
MAX_RELAY_PAYLOAD_BYTES = 65_536


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bounded(response, limit: int) -> bytes:
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(f"download exceeds frozen {limit}-byte bound")
    return payload


def _load_lock() -> dict[str, Any]:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if lock.get("schema") != "drand-verifier-lock-v1":
        raise ValueError("unexpected drand verifier lock schema")
    wrapper = TOOLS_DIR / lock["adapter"]["path"]
    if _sha256(wrapper.read_bytes()) != lock["adapter"]["sha256"]:
        raise ValueError("drand JavaScript adapter differs from the lock")
    return lock


def provision(cache_dir: Path) -> Path:
    """Download only the locked npm tarball and extract its locked ESM file."""

    lock = _load_lock()
    official = lock["official_verifier"]
    request = urllib.request.Request(
        official["tarball_url"],
        headers={"User-Agent": "pok-research-drand-verifier/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        tarball = _read_bounded(response, MAX_DOWNLOAD_BYTES)
    if _sha256(tarball) != official["tarball_sha256"]:
        raise ValueError("downloaded drand-client tarball digest mismatch")
    if hashlib.sha512(tarball).hexdigest() != official["tarball_sha512"]:
        raise ValueError("downloaded drand-client tarball SHA-512 mismatch")

    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / (
        f"drand-client-{official['version']}-index-"
        f"{official['module_sha256'][:16]}.mjs"
    )
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("cached drand verifier is not a regular file")
        if _sha256(destination.read_bytes()) != official["module_sha256"]:
            raise ValueError("cached drand verifier digest mismatch")
        return destination

    with tempfile.NamedTemporaryFile() as tar_fd:
        tar_fd.write(tarball)
        tar_fd.flush()
        with tarfile.open(tar_fd.name, mode="r:gz") as archive:
            member_name = official["module_tar_member"]
            member = archive.getmember(member_name)
            if not member.isfile() or member.size > MAX_DOWNLOAD_BYTES:
                raise ValueError("locked drand module is not a bounded regular file")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("locked drand module is absent from tarball")
            module = source.read(MAX_DOWNLOAD_BYTES + 1)
    if _sha256(module) != official["module_sha256"]:
        raise ValueError("drand-client ESM module digest mismatch")

    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        with temporary.open("xb") as output:
            output.write(module)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o444)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "pok-research-drand-cross-fetch/1",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise ValueError(f"drand relay returned HTTP {response.status}")
        return _read_bounded(response, MAX_RELAY_PAYLOAD_BYTES)


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def fetch_evidence(round_number: int, output_dir: Path) -> Path:
    """Fetch chain/current/previous payloads independently from three relays."""

    if isinstance(round_number, bool) or not isinstance(round_number, int) or round_number < 1:
        raise ValueError("round must be a positive integer")
    lock = _load_lock()
    chain_hash = lock["chain"]["chain_hash"]
    endpoints = tuple(lock["chain"]["endpoints"])
    if len(endpoints) < 3 or len(set(endpoints)) != len(endpoints):
        raise ValueError("lock must contain at least three distinct drand relays")

    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink() or any(output_dir.iterdir()):
        raise ValueError("evidence output directory must be empty and not a symlink")

    observations: list[dict[str, Any]] = []
    for index, endpoint in enumerate(endpoints, start=1):
        prefix = f"relay-{index}"
        info_name = f"{prefix}-info.json"
        current_name = f"{prefix}-round-{round_number}.json"
        info = _fetch(f"{endpoint}chains/{chain_hash}/info")
        current = _fetch(f"{endpoint}chains/{chain_hash}/rounds/{round_number}")
        _write_exclusive(output_dir / info_name, info)
        _write_exclusive(output_dir / current_name, current)
        row: dict[str, Any] = {
            "endpoint": endpoint,
            "chain_info_file": info_name,
            "chain_info_sha256": _sha256(info),
            "beacon_file": current_name,
            "beacon_sha256": _sha256(current),
        }
        if round_number > 1:
            previous_name = f"{prefix}-round-{round_number - 1}.json"
            previous = _fetch(
                f"{endpoint}chains/{chain_hash}/rounds/{round_number - 1}"
            )
            _write_exclusive(output_dir / previous_name, previous)
            row.update(
                {
                    "previous_beacon_file": previous_name,
                    "previous_beacon_sha256": _sha256(previous),
                }
            )
        observations.append(row)

    manifest = {
        "schema": "drand-cross-fetch-evidence-v1",
        "chain_hash": chain_hash,
        "round": round_number,
        "observations": observations,
    }
    manifest_path = output_dir / "evidence.json"
    _write_exclusive(
        manifest_path,
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        ),
    )
    return manifest_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    provision_parser = subparsers.add_parser("provision")
    provision_parser.add_argument("--cache-dir", type=Path, required=True)
    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("--round", type=int, required=True)
    fetch_parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "provision":
        result = provision(args.cache_dir)
    else:
        result = fetch_evidence(args.round, args.output_dir)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
