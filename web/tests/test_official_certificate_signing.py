from pathlib import Path
import subprocess

from official_certificate_signing import (
    SIGNATURE_NAMESPACE,
    SIGNER_PRINCIPAL,
    sign_certificate,
    signing_identity,
    signing_environment_report,
    verify_certificate_signature,
)


def _material(tmp_path: Path, name: str):
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    allowed = tmp_path / f"{name}.allowed"
    allowed.write_text(
        f"{SIGNER_PRINCIPAL} namespaces=\"{SIGNATURE_NAMESPACE}\" "
        + Path(str(key) + ".pub").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return key, allowed


def test_signature_binds_exact_certificate_content(tmp_path):
    key, allowed = _material(tmp_path, "issuer")
    record = {"schema_version": 4, "kind": "official-exe-compliance-certificate", "value": 1}

    signature = sign_certificate(record, key)

    verification = verify_certificate_signature(record, signature, allowed_signers=allowed)
    assert verification["valid"] is True
    assert verification["key_fingerprint"].startswith("SHA256:")
    tampered = {**record, "value": 2}
    assert verify_certificate_signature(tampered, signature, allowed_signers=allowed)["valid"] is False


def test_signature_rejects_untrusted_issuer(tmp_path):
    key, _allowed = _material(tmp_path, "issuer")
    _other_key, other_allowed = _material(tmp_path, "other")
    record = {"schema_version": 4, "kind": "official-exe-compliance-certificate"}

    signature = sign_certificate(record, key)

    assert verify_certificate_signature(record, signature, allowed_signers=other_allowed)["valid"] is False


def test_signing_environment_report_checks_private_key_against_trust_root(tmp_path, monkeypatch):
    key, allowed = _material(tmp_path, "issuer")
    monkeypatch.setenv("POK_OFFICIAL_SIGNING_KEY", str(key))

    report = signing_environment_report(allowed_signers=allowed)

    assert report["ok"] is True
    assert report["identity"]["key_fingerprint"].startswith("SHA256:")


def test_signing_environment_report_fails_for_untrusted_key(tmp_path, monkeypatch):
    key, _allowed = _material(tmp_path, "issuer")
    _other_key, other_allowed = _material(tmp_path, "other")
    monkeypatch.setenv("POK_OFFICIAL_SIGNING_KEY", str(key))

    report = signing_environment_report(allowed_signers=other_allowed)

    assert report["ok"] is False
    assert any("signature_invalid" in issue for issue in report["issues"])


def test_signing_identity_rejects_mismatched_public_key(tmp_path):
    key, _allowed = _material(tmp_path, "issuer")
    other_key, _other_allowed = _material(tmp_path, "other")
    Path(str(key) + ".pub").write_bytes(Path(str(other_key) + ".pub").read_bytes())

    try:
        signing_identity(key)
    except RuntimeError as exc:
        assert "private/public key mismatch" in str(exc)
    else:
        raise AssertionError("mismatched private/public key was accepted")


def test_environment_cannot_replace_production_trust_root(tmp_path, monkeypatch):
    import official_certificate_signing as signing

    attacker_key, attacker_allowed = _material(tmp_path, "attacker")
    record = {"schema_version": 4, "kind": "official-exe-compliance-certificate"}
    signature = sign_certificate(record, attacker_key)
    monkeypatch.setenv("POK_OFFICIAL_SIGNING_KEY", str(attacker_key))
    monkeypatch.setenv("POK_OFFICIAL_ALLOWED_SIGNERS", str(attacker_allowed))

    default_result = verify_certificate_signature(record, signature)
    explicit_test_result = verify_certificate_signature(
        record,
        signature,
        allowed_signers=attacker_allowed,
    )

    assert signing.allowed_signers_path() == signing.DEFAULT_ALLOWED_SIGNERS
    assert default_result["valid"] is False
    assert explicit_test_result["valid"] is True
    assert signing_environment_report()["ok"] is False
