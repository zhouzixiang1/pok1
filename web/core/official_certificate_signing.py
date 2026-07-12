"""OpenSSH Ed25519 signing with repository-owned signer epoch policy.

The OpenSSH ``allowed_signers`` file proves that a signature came from one of
the listed public keys.  It does not say whether that key is still authorized
to issue *new* authority.  This module adds that application-level boundary:

* exactly one tracked signer epoch may be active for issuance;
* retired keys are validation-only and are limited to exact record/signature
  hashes recorded in the tracked policy; and
* every new certificate, ledger entry, and signed ledger head binds the active
  epoch, fingerprint, and policy digest into the signed payload.

Production trust paths are deliberately not environment-overridable.  Tests
may pass explicit paths or monkeypatch the tracked-path constants.

Threat-model boundary: the current operator, orchestrator, and Claude SDK
processes run under one OS uid.  Mode ``0600`` therefore does not isolate the
private key from an agent that can execute Bash as that uid.  These signatures
prove byte integrity and publication binding to keyless verifiers; they are not
evidence that a malicious same-uid agent could not issue a signature.  A future
same-uid-adversary claim requires a separate OS identity and a validating,
non-generic signer service.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
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
DEFAULT_SIGNING_KEY = (
    Path.home() / ".config" / "pok" / "official_certifier_ed25519_epoch2"
)
DEFAULT_ALLOWED_SIGNERS = ROOT / "web" / "core" / "official_certifier_allowed_signers"
DEFAULT_TRUST_POLICY = ROOT / "web" / "core" / "official_certifier_trust_policy.json"

TRUST_POLICY_SCHEMA_VERSION = 1
TRUST_POLICY_KIND = "official-certifier-signer-epoch-policy"
CURRENT_SIGNER_ACTIVE = "active"
CURRENT_SIGNER_ROTATION_REQUIRED = "rotation-required"
HISTORICAL_SIGNER_VALIDATION_ONLY = "historical-validation-only"
SIGNER_THREAT_MODEL = {
    "scope": "formal-publication-authentication-for-an-independently-anchored-public-key",
    "same_uid_llm_resistance": False,
    "private_key_isolation": "not-provided-by-current-single-uid-host",
    "out_of_band_public_key_anchor": "required-for-independent-verification",
    "runtime_or_strength_authority": "none",
}

_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/=]+$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_PURPOSE_BY_KIND = {
    "official-exe-compliance-certificate": "certificate",
    "official-exe-verdict-ledger-entry": "ledger-entry",
    "official-exe-verdict-ledger-head": "ledger-head",
    "official-certificate-signing-readiness": "readiness",
}


class SignerTrustPolicyError(RuntimeError):
    """The repository-owned signer policy is missing or internally invalid."""


def signing_key_path() -> Path:
    return Path(os.environ.get("POK_OFFICIAL_SIGNING_KEY", str(DEFAULT_SIGNING_KEY))).expanduser()


def allowed_signers_path() -> Path:
    """Return the repository-owned OpenSSH public-key inventory."""
    return DEFAULT_ALLOWED_SIGNERS


def signer_trust_policy_path() -> Path:
    """Return the repository-owned application authorization policy."""
    return DEFAULT_TRUST_POLICY


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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _policy_digest(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "policy_digest"}
    return _sha256_bytes(certificate_bytes(unsigned))


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


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SignerTrustPolicyError(f"{label} fields invalid")


def _hex64(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX64_RE.fullmatch(value))


def _fingerprint(value: object) -> bool:
    return isinstance(value, str) and bool(_FINGERPRINT_RE.fullmatch(value))


def _read_policy(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SignerTrustPolicyError("official signer trust policy missing or not regular")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SignerTrustPolicyError(
            f"official signer trust policy unreadable:{type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise SignerTrustPolicyError("official signer trust policy must be an object")
    return payload


def _validate_historical_chain(chain: object, label: str) -> dict[str, Any]:
    if not isinstance(chain, dict):
        raise SignerTrustPolicyError(f"{label} historical chain missing")
    expected = {
        "candidate_label",
        "candidate_hash",
        "certificate_digest",
        "evidence_archive_sha256",
        "evidence_archive_manifest_digest",
        "evidence_sha256",
        "ledger_sequence",
        "ledger_entry_digest",
        "ledger_head_size_bytes",
        "bootstrap_root_id",
        "bootstrap_manifest_file_sha256",
        "bootstrap_manifest_canonical_sha256",
    }
    _exact_keys(chain, expected, f"{label} historical chain")
    if not isinstance(chain["candidate_label"], str) or not chain["candidate_label"]:
        raise SignerTrustPolicyError(f"{label} candidate label invalid")
    if not isinstance(chain["bootstrap_root_id"], str) or not chain["bootstrap_root_id"]:
        raise SignerTrustPolicyError(f"{label} bootstrap root id invalid")
    for field in expected - {
        "candidate_label",
        "bootstrap_root_id",
        "ledger_sequence",
        "ledger_head_size_bytes",
    }:
        if not _hex64(chain[field]):
            raise SignerTrustPolicyError(f"{label} {field} invalid")
    if type(chain["ledger_sequence"]) is not int or chain["ledger_sequence"] <= 0:
        raise SignerTrustPolicyError(f"{label} ledger sequence invalid")
    if type(chain["ledger_head_size_bytes"]) is not int or chain["ledger_head_size_bytes"] <= 0:
        raise SignerTrustPolicyError(f"{label} ledger head size invalid")
    return chain


def load_signer_trust_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load and strictly validate the tracked signer epoch policy."""
    target = Path(path) if path is not None else signer_trust_policy_path()
    payload = _read_policy(target)
    _exact_keys(
        payload,
        {
            "schema_version",
            "kind",
            "policy_id",
            "current_epoch",
            "current_signer",
            "historical_signers",
            "policy_digest",
        },
        "official signer trust policy",
    )
    if payload.get("schema_version") != TRUST_POLICY_SCHEMA_VERSION:
        raise SignerTrustPolicyError("official signer trust policy schema invalid")
    if payload.get("kind") != TRUST_POLICY_KIND:
        raise SignerTrustPolicyError("official signer trust policy kind invalid")
    if not isinstance(payload.get("policy_id"), str) or not payload["policy_id"]:
        raise SignerTrustPolicyError("official signer trust policy id missing")
    if payload.get("policy_digest") != _policy_digest(payload):
        raise SignerTrustPolicyError("official signer trust policy digest mismatch")
    current_epoch = payload.get("current_epoch")
    if type(current_epoch) is not int or current_epoch <= 0:
        raise SignerTrustPolicyError("official signer current epoch invalid")
    current = payload.get("current_signer")
    if not isinstance(current, dict):
        raise SignerTrustPolicyError("official signer current authority missing")
    _exact_keys(
        current,
        {"epoch", "state", "key_fingerprint", "public_key_sha256"},
        "official signer current authority",
    )
    if current.get("epoch") != current_epoch:
        raise SignerTrustPolicyError("official signer current epoch mismatch")
    state = current.get("state")
    if state == CURRENT_SIGNER_ACTIVE:
        if not _fingerprint(current.get("key_fingerprint")):
            raise SignerTrustPolicyError("official signer current fingerprint invalid")
        if not _hex64(current.get("public_key_sha256")):
            raise SignerTrustPolicyError("official signer current public key digest invalid")
    elif state == CURRENT_SIGNER_ROTATION_REQUIRED:
        if current.get("key_fingerprint") is not None or current.get("public_key_sha256") is not None:
            raise SignerTrustPolicyError("rotation-required signer must not name a key")
    else:
        raise SignerTrustPolicyError("official signer current state invalid")

    historical = payload.get("historical_signers")
    if not isinstance(historical, list) or not historical:
        raise SignerTrustPolicyError("official signer historical authority missing")
    epochs = {current_epoch}
    fingerprints: set[str] = set()
    if state == CURRENT_SIGNER_ACTIVE:
        fingerprints.add(str(current["key_fingerprint"]))
    for index, signer in enumerate(historical):
        label = f"historical signer {index}"
        if not isinstance(signer, dict):
            raise SignerTrustPolicyError(f"{label} invalid")
        _exact_keys(
            signer,
            {
                "epoch",
                "state",
                "key_fingerprint",
                "public_key_sha256",
                "allowed_records",
                "historical_chain",
            },
            label,
        )
        epoch = signer.get("epoch")
        if type(epoch) is not int or epoch <= 0 or epoch >= current_epoch or epoch in epochs:
            raise SignerTrustPolicyError(f"{label} epoch invalid")
        epochs.add(epoch)
        if signer.get("state") != HISTORICAL_SIGNER_VALIDATION_ONLY:
            raise SignerTrustPolicyError(f"{label} state invalid")
        signer_fingerprint = signer.get("key_fingerprint")
        if not _fingerprint(signer_fingerprint) or signer_fingerprint in fingerprints:
            raise SignerTrustPolicyError(f"{label} fingerprint invalid")
        fingerprints.add(str(signer_fingerprint))
        if not _hex64(signer.get("public_key_sha256")):
            raise SignerTrustPolicyError(f"{label} public key digest invalid")
        records = signer.get("allowed_records")
        if not isinstance(records, list) or not records:
            raise SignerTrustPolicyError(f"{label} allowed records missing")
        purposes: set[str] = set()
        for record_index, record in enumerate(records):
            record_label = f"{label} record {record_index}"
            if not isinstance(record, dict):
                raise SignerTrustPolicyError(f"{record_label} invalid")
            _exact_keys(
                record,
                {"purpose", "record_sha256", "signature_sha256"},
                record_label,
            )
            purpose = record.get("purpose")
            if purpose not in {"certificate", "ledger-entry", "ledger-head"} or purpose in purposes:
                raise SignerTrustPolicyError(f"{record_label} purpose invalid")
            purposes.add(str(purpose))
            if not _hex64(record.get("record_sha256")) or not _hex64(record.get("signature_sha256")):
                raise SignerTrustPolicyError(f"{record_label} digest invalid")
        _validate_historical_chain(signer.get("historical_chain"), label)
    return payload


def _public_key_material(public_key: Path) -> tuple[str, str, str]:
    if public_key.is_symlink() or not public_key.is_file():
        raise FileNotFoundError(f"official signing public key missing: {public_key}")
    tokens = public_key.read_text(encoding="utf-8").split()
    if len(tokens) < 2 or tokens[0] != "ssh-ed25519":
        raise RuntimeError("official signing public key must be OpenSSH Ed25519")
    normalized = f"{tokens[0]} {tokens[1]}\n"
    fingerprint = _run(["ssh-keygen", "-lf", str(public_key), "-E", "sha256"])
    if fingerprint.returncode != 0:
        raise RuntimeError(
            "ssh-keygen fingerprint failed: "
            + fingerprint.stderr.decode(errors="replace")[:300]
        )
    fingerprint_tokens = fingerprint.stdout.decode("utf-8", errors="replace").split()
    key_fingerprint = next(
        (token for token in fingerprint_tokens if token.startswith("SHA256:")), ""
    )
    if not _fingerprint(key_fingerprint):
        raise RuntimeError("official signing key fingerprint unavailable")
    return normalized, key_fingerprint, _sha256_bytes(normalized.encode("utf-8"))


@lru_cache(maxsize=32)
def _raw_signing_identity_snapshot(
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
    _normalized, key_fingerprint, public_key_sha256 = _public_key_material(public_key)
    if _stat_identity(key) != key_snapshot or _stat_identity(public_key) != public_snapshot:
        raise RuntimeError("official signing key material changed during identity check")
    return {
        "principal": SIGNER_PRINCIPAL,
        "namespace": SIGNATURE_NAMESPACE,
        "key_fingerprint": key_fingerprint,
        "public_key_sha256": public_key_sha256,
        "public_key_file_sha256": _sha256_bytes(public_key.read_bytes()),
    }


def signing_identity(
    key_path: str | Path | None = None,
    *,
    trust_policy: str | Path | None = None,
) -> dict[str, Any]:
    """Return the active signing identity or fail closed during rotation."""
    policy = load_signer_trust_policy(trust_policy)
    current = policy["current_signer"]
    if current["state"] != CURRENT_SIGNER_ACTIVE:
        # Do not even open/derive the retired private key while production is
        # deliberately parked for rotation.
        raise SignerTrustPolicyError("official signer rotation is required before issuance")
    key = Path(key_path) if key_path is not None else signing_key_path()
    public_key = Path(str(key) + ".pub")
    if key.is_symlink() or not key.is_file():
        raise FileNotFoundError(f"official certificate signing key missing: {key}")
    if public_key.is_symlink() or not public_key.is_file():
        raise FileNotFoundError(f"official certificate public key missing: {public_key}")
    mode = key.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(
            f"official certificate signing key permissions must be 0600: {oct(mode)}"
        )
    identity = dict(
        _raw_signing_identity_snapshot(
            str(key.resolve()),
            _stat_identity(key),
            _stat_identity(public_key),
        )
    )
    if identity["key_fingerprint"] != current["key_fingerprint"]:
        raise SignerTrustPolicyError("official signing key is not the current signer epoch")
    if identity["public_key_sha256"] != current["public_key_sha256"]:
        raise SignerTrustPolicyError("official signing public key digest does not match policy")
    return {
        **identity,
        "signer_epoch": current["epoch"],
        "trust_policy_id": policy["policy_id"],
        "trust_policy_digest": policy["policy_digest"],
    }


def current_signer_record_fields(
    key_path: str | Path | None = None,
    *,
    trust_policy: str | Path | None = None,
) -> dict[str, Any]:
    identity = signing_identity(key_path, trust_policy=trust_policy)
    return {
        "signer_epoch": identity["signer_epoch"],
        "signer_key_fingerprint": identity["key_fingerprint"],
        "signer_policy_id": identity["trust_policy_id"],
        "signer_policy_digest": identity["trust_policy_digest"],
    }


def _signature_purpose(record: dict[str, Any]) -> str | None:
    return _PURPOSE_BY_KIND.get(str(record.get("kind") or ""))


def _current_record_binding_issues(
    record: dict[str, Any], identity: dict[str, Any]
) -> list[str]:
    purpose = _signature_purpose(record)
    if purpose is None:
        return ["official_signature_record_kind_not_authorized"]
    if purpose == "certificate":
        issuer = record.get("issuer")
        issuer = issuer if isinstance(issuer, dict) else {}
        expected = {
            "principal": SIGNER_PRINCIPAL,
            "namespace": SIGNATURE_NAMESPACE,
            "key_fingerprint": identity["key_fingerprint"],
            "public_key_sha256": identity["public_key_sha256"],
            "signer_epoch": identity["signer_epoch"],
            "trust_policy_id": identity["trust_policy_id"],
            "trust_policy_digest": identity["trust_policy_digest"],
        }
        return [
            f"official_certificate_signer_binding_mismatch:{key}"
            for key, value in expected.items()
            if issuer.get(key) != value
        ]
    expected_fields = {
        "signer_epoch": identity["signer_epoch"],
        "signer_key_fingerprint": identity["key_fingerprint"],
        "signer_policy_id": identity["trust_policy_id"],
        "signer_policy_digest": identity["trust_policy_digest"],
    }
    return [
        f"official_signature_signer_binding_mismatch:{key}"
        for key, value in expected_fields.items()
        if record.get(key) != value
    ]


def bind_current_signer(
    record: dict[str, Any],
    key_path: str | Path | None = None,
    *,
    trust_policy: str | Path | None = None,
) -> dict[str, Any]:
    """Return a copy with the active signer epoch embedded before signing."""
    identity = signing_identity(key_path, trust_policy=trust_policy)
    purpose = _signature_purpose(record)
    if purpose is None:
        raise SignerTrustPolicyError("official signature record kind is not authorized")
    if purpose == "certificate":
        return {**record, "issuer": identity}
    return {
        **record,
        "signer_epoch": identity["signer_epoch"],
        "signer_key_fingerprint": identity["key_fingerprint"],
        "signer_policy_id": identity["trust_policy_id"],
        "signer_policy_digest": identity["trust_policy_digest"],
    }


def sign_certificate(
    record: dict[str, Any],
    key_path: str | Path | None = None,
    *,
    trust_policy: str | Path | None = None,
) -> str:
    """Sign only with the active epoch and only an epoch-bound payload."""
    key = Path(key_path) if key_path is not None else signing_key_path()
    identity = signing_identity(key, trust_policy=trust_policy)
    binding_issues = _current_record_binding_issues(record, identity)
    if binding_issues:
        raise SignerTrustPolicyError(
            "official record is not bound to current signer: " + ", ".join(binding_issues)
        )
    result = _run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", SIGNATURE_NAMESPACE, "-"],
        input_bytes=certificate_bytes(record),
    )
    signature = result.stdout.decode("utf-8", errors="strict")
    if result.returncode != 0 or "BEGIN SSH SIGNATURE" not in signature:
        raise RuntimeError(
            "official certificate signing failed: "
            + result.stderr.decode(errors="replace")[:300]
        )
    return signature


def _raw_signature_verification(
    record: dict[str, Any], signature: str, trust: Path
) -> tuple[list[str], str]:
    issues: list[str] = []
    if trust.is_symlink() or not trust.is_file():
        return ["official_certificate_trust_root_missing"], ""
    if "BEGIN SSH SIGNATURE" not in str(signature):
        return ["official_certificate_signature_missing"], ""
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
    match = re.search(r"SHA256:[A-Za-z0-9+/=]+", verification_text)
    verified_fingerprint = match.group(0) if match else ""
    if not issues and not verified_fingerprint:
        issues.append("official_certificate_verified_key_fingerprint_missing")
    return issues, verified_fingerprint


def _historical_authorization(
    policy: dict[str, Any],
    fingerprint: str,
    record: dict[str, Any],
    signature: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    signer = next(
        (
            item
            for item in policy["historical_signers"]
            if item.get("key_fingerprint") == fingerprint
        ),
        None,
    )
    if signer is None:
        return None, ["official_signature_signer_not_authorized"]
    purpose = _signature_purpose(record)
    record_sha256 = _sha256_bytes(certificate_bytes(record))
    signature_sha256 = _sha256_bytes(signature.encode("utf-8"))
    allowed = next(
        (
            item
            for item in signer["allowed_records"]
            if item["purpose"] == purpose
            and item["record_sha256"] == record_sha256
            and item["signature_sha256"] == signature_sha256
        ),
        None,
    )
    if allowed is None:
        return signer, ["official_historical_signature_outside_exact_exception"]
    return signer, []


def verify_certificate_signature(
    record: dict[str, Any],
    signature: str,
    *,
    allowed_signers: str | Path | None = None,
    trust_policy: str | Path | None = None,
) -> dict[str, Any]:
    """Verify cryptography *and* signer-epoch authorization."""
    trust = Path(allowed_signers) if allowed_signers is not None else allowed_signers_path()
    issues, verified_fingerprint = _raw_signature_verification(record, signature, trust)
    signer_epoch: int | None = None
    signer_state = ""
    policy_id = ""
    policy_digest = ""
    try:
        policy = load_signer_trust_policy(trust_policy)
        policy_id = str(policy["policy_id"])
        policy_digest = str(policy["policy_digest"])
        current = policy["current_signer"]
        if verified_fingerprint and (
            current["state"] == CURRENT_SIGNER_ACTIVE
            and verified_fingerprint == current["key_fingerprint"]
        ):
            signer_epoch = int(current["epoch"])
            signer_state = CURRENT_SIGNER_ACTIVE
            identity = {
                "key_fingerprint": current["key_fingerprint"],
                "public_key_sha256": current["public_key_sha256"],
                "signer_epoch": current["epoch"],
                "trust_policy_id": policy["policy_id"],
                "trust_policy_digest": policy["policy_digest"],
            }
            issues.extend(_current_record_binding_issues(record, identity))
        elif verified_fingerprint:
            historical, authorization_issues = _historical_authorization(
                policy, verified_fingerprint, record, signature
            )
            issues.extend(authorization_issues)
            if historical is not None:
                signer_epoch = int(historical["epoch"])
                signer_state = HISTORICAL_SIGNER_VALIDATION_ONLY
    except Exception as exc:
        issues.append(
            f"official_signer_trust_policy_invalid:{type(exc).__name__}:{str(exc)[:180]}"
        )
    return {
        "valid": not issues,
        "issues": list(dict.fromkeys(issues)),
        "principal": SIGNER_PRINCIPAL,
        "namespace": SIGNATURE_NAMESPACE,
        "key_fingerprint": verified_fingerprint,
        "signer_epoch": signer_epoch,
        "signer_state": signer_state,
        "trust_policy_id": policy_id,
        "trust_policy_digest": policy_digest,
    }


def historical_bootstrap_root_binding(
    root_id: str, *, trust_policy: str | Path | None = None
) -> dict[str, Any] | None:
    """Return the unique retired-signer chain that authorizes ``root_id``."""
    policy = load_signer_trust_policy(trust_policy)
    matches = [
        deepcopy(item["historical_chain"])
        for item in policy["historical_signers"]
        if item["historical_chain"].get("bootstrap_root_id") == root_id
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def signing_environment_report(
    *,
    allowed_signers: str | Path | None = None,
    trust_policy: str | Path | None = None,
) -> dict[str, Any]:
    """Check current epoch key, policy, and trust inventory before EXE work."""
    trust_root = (
        Path(allowed_signers).expanduser()
        if allowed_signers is not None
        else allowed_signers_path()
    )
    policy_path = (
        Path(trust_policy).expanduser()
        if trust_policy is not None
        else signer_trust_policy_path()
    )
    try:
        identity = signing_identity(trust_policy=policy_path)
        probe = bind_current_signer(
            {
                "schema_version": 2,
                "kind": "official-certificate-signing-readiness",
                "namespace": SIGNATURE_NAMESPACE,
            },
            trust_policy=policy_path,
        )
        signature = sign_certificate(probe, trust_policy=policy_path)
        verification = verify_certificate_signature(
            probe,
            signature,
            allowed_signers=trust_root,
            trust_policy=policy_path,
        )
        issues = list(verification.get("issues") or [])
        if verification.get("key_fingerprint") != identity.get("key_fingerprint"):
            issues.append("official_certificate_signer_fingerprint_mismatch")
        return {
            "ok": bool(verification.get("valid")) and not issues,
            "identity": identity,
            "trust_root": str(trust_root),
            "trust_policy": str(policy_path),
            "threat_model": dict(SIGNER_THREAT_MODEL),
            "issues": list(dict.fromkeys(issues)),
        }
    except Exception as exc:
        return {
            "ok": False,
            "identity": None,
            "trust_root": str(trust_root),
            "trust_policy": str(policy_path),
            "threat_model": dict(SIGNER_THREAT_MODEL),
            "issues": [
                f"official_certificate_signing_unavailable:{type(exc).__name__}:{str(exc)[:240]}"
            ],
        }


def build_signer_rotation_material(
    public_key: str | Path,
    *,
    trust_policy: str | Path | None = None,
    allowed_signers: str | Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Build reviewed tracked outputs from an already-created public key.

    This function never creates, reads, or overwrites a private key.  It only
    accepts a public key and a policy whose current state is explicitly
    ``rotation-required``.
    """
    policy = deepcopy(load_signer_trust_policy(trust_policy))
    if policy["current_signer"]["state"] != CURRENT_SIGNER_ROTATION_REQUIRED:
        raise SignerTrustPolicyError("signer policy is not awaiting a rotation")
    public_path = Path(public_key).expanduser()
    normalized, fingerprint, public_key_sha256 = _public_key_material(public_path)
    retired = {item["key_fingerprint"] for item in policy["historical_signers"]}
    if fingerprint in retired:
        raise SignerTrustPolicyError("new signer must not reuse a retired key")
    policy["current_signer"] = {
        "epoch": policy["current_epoch"],
        "state": CURRENT_SIGNER_ACTIVE,
        "key_fingerprint": fingerprint,
        "public_key_sha256": public_key_sha256,
    }
    policy["policy_digest"] = _policy_digest(policy)

    allowed_path = (
        Path(allowed_signers).expanduser()
        if allowed_signers is not None
        else allowed_signers_path()
    )
    if allowed_path.is_symlink() or not allowed_path.is_file():
        raise SignerTrustPolicyError("allowed-signers inventory missing or not regular")
    existing = allowed_path.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        raise SignerTrustPolicyError("allowed-signers inventory is not newline terminated")
    new_line = (
        f'{SIGNER_PRINCIPAL} namespaces="{SIGNATURE_NAMESPACE}" '
        f"{normalized.rstrip()} {SIGNER_PRINCIPAL}-epoch-{policy['current_epoch']}\n"
    )
    if normalized.split()[1] in existing:
        raise SignerTrustPolicyError("new signer public key already exists in inventory")
    return policy, existing + new_line


def _write_exclusive(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        os.fchmod(descriptor, mode)
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def render_signer_rotation(
    public_key: str | Path,
    output_dir: str | Path,
    *,
    trust_policy: str | Path | None = None,
    allowed_signers: str | Path | None = None,
) -> dict[str, Any]:
    """Write non-production candidate trust files to a fresh review directory."""
    policy, allowed = build_signer_rotation_material(
        public_key,
        trust_policy=trust_policy,
        allowed_signers=allowed_signers,
    )
    destination = Path(output_dir).expanduser()
    if destination.is_symlink():
        raise SignerTrustPolicyError("rotation output directory must not be a symlink")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    policy_output = destination / DEFAULT_TRUST_POLICY.name
    allowed_output = destination / DEFAULT_ALLOWED_SIGNERS.name
    _write_exclusive(
        policy_output,
        (json.dumps(policy, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        0o600,
    )
    try:
        _write_exclusive(allowed_output, allowed.encode("utf-8"), 0o600)
    except Exception:
        policy_output.unlink(missing_ok=True)
        raise
    directory_fd = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {
        "current_epoch": policy["current_epoch"],
        "key_fingerprint": policy["current_signer"]["key_fingerprint"],
        "policy_digest": policy["policy_digest"],
        "policy_output": str(policy_output),
        "allowed_signers_output": str(allowed_output),
        "private_key_touched": False,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Render reviewed official signer rotation trust files; never creates a private key."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render-rotation")
    render.add_argument("--public-key", required=True)
    render.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    if args.command == "render-rotation":
        result = render_signer_rotation(args.public_key, args.output_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
