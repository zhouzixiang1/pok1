"""OpenSSH Ed25519 trust boundary for official compliance certificates."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SIGNATURE_NAMESPACE = "pok-official-cert-v4"
SIGNER_PRINCIPAL = "pok-official-certifier"
DEFAULT_SIGNING_KEY = Path.home() / ".config" / "pok" / "official_certifier_ed25519"
DEFAULT_ALLOWED_SIGNERS = ROOT / "web" / "core" / "official_certifier_allowed_signers"


def signing_key_path() -> Path:
    return Path(os.environ.get("POK_OFFICIAL_SIGNING_KEY", str(DEFAULT_SIGNING_KEY))).expanduser()


def allowed_signers_path() -> Path:
    """Return the repository-owned production trust root.

    The signing key is operator-local, but certificate verification policy is
    tracked source.  An environment variable must not be able to replace both
    sides of that trust boundary.  Tests that need an alternate issuer pass an
    explicit ``allowed_signers`` argument or monkeypatch this constant.
    """
    return DEFAULT_ALLOWED_SIGNERS


def certificate_bytes(record: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _run(command: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _stat_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    metadata = path.stat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@lru_cache(maxsize=32)
def _signing_identity_snapshot(
    key_name: str,
    key_snapshot: tuple[int, int, int, int, int, int],
    public_snapshot: tuple[int, int, int, int, int, int],
) -> dict[str, str]:
    key = Path(key_name)
    public_key = Path(str(key) + ".pub")
    derived = _run(["ssh-keygen", "-y", "-f", str(key)])
    if derived.returncode != 0:
        raise RuntimeError(
            "official private-key public derivation failed: "
            + derived.stderr.decode(errors="replace")[:300]
        )
    derived_tokens = derived.stdout.decode("utf-8", errors="strict").split()
    published_tokens = public_key.read_text(encoding="utf-8").split()
    if len(derived_tokens) < 2 or len(published_tokens) < 2:
        raise RuntimeError("official signing public key material is invalid")
    if derived_tokens[:2] != published_tokens[:2]:
        raise RuntimeError("official signing private/public key mismatch")
    fingerprint = _run(["ssh-keygen", "-lf", str(public_key), "-E", "sha256"])
    if fingerprint.returncode != 0:
        raise RuntimeError(f"ssh-keygen fingerprint failed: {fingerprint.stderr.decode(errors='replace')[:300]}")
    tokens = fingerprint.stdout.decode("utf-8", errors="replace").split()
    key_fingerprint = next((token for token in tokens if token.startswith("SHA256:")), "")
    if not key_fingerprint:
        raise RuntimeError("official signing key fingerprint unavailable")
    if _stat_identity(key) != key_snapshot or _stat_identity(public_key) != public_snapshot:
        raise RuntimeError("official signing key material changed during identity check")
    return {
        "principal": SIGNER_PRINCIPAL,
        "namespace": SIGNATURE_NAMESPACE,
        "key_fingerprint": key_fingerprint,
        "public_key_sha256": hashlib.sha256(public_key.read_bytes()).hexdigest(),
    }


def signing_identity(key_path: str | Path | None = None) -> dict[str, str]:
    key = Path(key_path) if key_path is not None else signing_key_path()
    public_key = Path(str(key) + ".pub")
    if key.is_symlink() or not key.is_file():
        raise FileNotFoundError(f"official certificate signing key missing: {key}")
    if public_key.is_symlink() or not public_key.is_file():
        raise FileNotFoundError(f"official certificate public key missing: {public_key}")
    mode = key.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(f"official certificate signing key permissions must be 0600: {oct(mode)}")
    return dict(
        _signing_identity_snapshot(
            str(key.resolve()),
            _stat_identity(key),
            _stat_identity(public_key),
        )
    )


def sign_certificate(record: dict[str, Any], key_path: str | Path | None = None) -> str:
    key = Path(key_path) if key_path is not None else signing_key_path()
    signing_identity(key)
    result = _run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", SIGNATURE_NAMESPACE, "-"],
        input_bytes=certificate_bytes(record),
    )
    signature = result.stdout.decode("utf-8", errors="strict")
    if result.returncode != 0 or "BEGIN SSH SIGNATURE" not in signature:
        raise RuntimeError(f"official certificate signing failed: {result.stderr.decode(errors='replace')[:300]}")
    return signature


def verify_certificate_signature(
    record: dict[str, Any],
    signature: str,
    *,
    allowed_signers: str | Path | None = None,
) -> dict[str, Any]:
    trust = Path(allowed_signers) if allowed_signers is not None else allowed_signers_path()
    issues: list[str] = []
    if trust.is_symlink() or not trust.is_file():
        return {"valid": False, "issues": ["official_certificate_trust_root_missing"]}
    if "BEGIN SSH SIGNATURE" not in str(signature):
        return {"valid": False, "issues": ["official_certificate_signature_missing"]}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".sig") as handle:
        handle.write(signature)
        handle.flush()
        result = _run(
            [
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(trust),
                "-I",
                SIGNER_PRINCIPAL,
                "-n",
                SIGNATURE_NAMESPACE,
                "-s",
                handle.name,
            ],
            input_bytes=certificate_bytes(record),
        )
    if result.returncode != 0:
        issues.append(
            "official_certificate_signature_invalid: "
            + result.stderr.decode("utf-8", errors="replace")[:240]
        )
    verification_text = (
        result.stdout.decode("utf-8", errors="replace")
        + "\n"
        + result.stderr.decode("utf-8", errors="replace")
    )
    fingerprint_match = re.search(r"SHA256:[A-Za-z0-9+/=]+", verification_text)
    verified_fingerprint = fingerprint_match.group(0) if fingerprint_match else ""
    if not issues and not verified_fingerprint:
        issues.append("official_certificate_verified_key_fingerprint_missing")
    return {
        "valid": not issues,
        "issues": issues,
        "principal": SIGNER_PRINCIPAL,
        "namespace": SIGNATURE_NAMESPACE,
        "key_fingerprint": verified_fingerprint,
    }


def signing_environment_report(
    *,
    allowed_signers: str | Path | None = None,
) -> dict[str, Any]:
    """Check issuer key and repository trust root before an expensive EXE suite."""
    try:
        identity = signing_identity()
        probe = {
            "schema_version": 1,
            "kind": "official-certificate-signing-readiness",
            "namespace": SIGNATURE_NAMESPACE,
        }
        signature = sign_certificate(probe)
        trust_root = (
            Path(allowed_signers).expanduser()
            if allowed_signers is not None
            else allowed_signers_path()
        )
        verification = verify_certificate_signature(
            probe,
            signature,
            allowed_signers=trust_root,
        )
        issues = list(verification.get("issues") or [])
        if verification.get("key_fingerprint") != identity.get("key_fingerprint"):
            issues.append("official_certificate_signer_fingerprint_mismatch")
        return {
            "ok": bool(verification.get("valid")) and not issues,
            "identity": identity,
            "trust_root": str(trust_root),
            "issues": issues,
        }
    except Exception as exc:
        return {
            "ok": False,
            "identity": None,
            "trust_root": str(
                Path(allowed_signers).expanduser()
                if allowed_signers is not None
                else allowed_signers_path()
            ),
            "issues": [f"official_certificate_signing_unavailable:{type(exc).__name__}:{str(exc)[:240]}"],
        }
