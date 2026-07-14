from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

import official_certificate_signing as signing
from official_certificate_signing import (
    SIGNATURE_NAMESPACE,
    SIGNER_PRINCIPAL,
    bind_current_signer,
    build_signer_rotation_material,
    certificate_bytes,
    load_signer_trust_policy,
    render_signer_rotation,
    sign_certificate,
    signing_environment_report,
    signing_identity,
    verify_certificate_signature,
)


def _key(tmp_path: Path, name: str) -> Path:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    return key


def _active_material(tmp_path: Path, name: str = "issuer"):
    key = _key(tmp_path, name)
    pending = deepcopy(load_signer_trust_policy())
    pending["current_signer"] = {
        "epoch": pending["current_epoch"],
        "state": "rotation-required",
        "key_fingerprint": None,
        "public_key_sha256": None,
    }
    pending["policy_digest"] = signing._policy_digest(pending)
    pending_path = tmp_path / f"{name}.pending.json"
    pending_path.write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")
    policy_payload, allowed_payload = build_signer_rotation_material(
        Path(str(key) + ".pub"), trust_policy=pending_path
    )
    policy = tmp_path / f"{name}.policy.json"
    policy.write_text(json.dumps(policy_payload, indent=2) + "\n", encoding="utf-8")
    allowed = tmp_path / f"{name}.allowed"
    allowed.write_text(allowed_payload, encoding="utf-8")
    return key, allowed, policy


def _certificate_record(key: Path, policy: Path, *, value: int = 1):
    return bind_current_signer(
        {
            "schema_version": 5,
            "kind": "official-exe-compliance-certificate",
            "candidate_label": "national_v999",
            "identity": {"candidate_hash": "a" * 64},
            "certificate_digest": "b" * 64,
            "evidence_archive": None,
            "value": value,
        },
        key,
        trust_policy=policy,
    )


def _public_identity(public_key: Path) -> tuple[str, str]:
    tokens = public_key.read_text(encoding="utf-8").split()
    normalized = f"{tokens[0]} {tokens[1]}\n"
    result = subprocess.run(
        ["ssh-keygen", "-lf", str(public_key), "-E", "sha256"],
        check=True,
        capture_output=True,
        text=True,
    )
    fingerprint = next(token for token in result.stdout.split() if token.startswith("SHA256:"))
    return fingerprint, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _raw_sign(record: dict, key: Path) -> str:
    result = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", SIGNATURE_NAMESPACE, "-"],
        input=certificate_bytes(record),
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8")


def _historical_exception_material(tmp_path: Path):
    old_key = _key(tmp_path, "retired")
    current_key = _key(tmp_path, "current")
    old_record = {
        "schema_version": 1,
        "kind": "official-exe-verdict-ledger-entry",
        "sequence": 1,
        "entry_digest": "1" * 64,
    }
    old_signature = _raw_sign(old_record, old_key)
    old_fingerprint, old_public_sha = _public_identity(Path(str(old_key) + ".pub"))
    pending = deepcopy(load_signer_trust_policy())
    pending["historical_signers"][0]["key_fingerprint"] = old_fingerprint
    pending["historical_signers"][0]["public_key_sha256"] = old_public_sha
    pending["historical_signers"][0]["allowed_records"] = [
        {
            "purpose": "ledger-entry",
            "record_sha256": hashlib.sha256(certificate_bytes(old_record)).hexdigest(),
            "signature_sha256": hashlib.sha256(old_signature.encode("utf-8")).hexdigest(),
        }
    ]
    pending["current_signer"] = {
        "epoch": pending["current_epoch"],
        "state": "rotation-required",
        "key_fingerprint": None,
        "public_key_sha256": None,
    }
    pending["policy_digest"] = signing._policy_digest(pending)
    pending_path = tmp_path / "pending.json"
    pending_path.write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")
    old_tokens = Path(str(old_key) + ".pub").read_text(encoding="utf-8").split()
    old_allowed = tmp_path / "old.allowed"
    old_allowed.write_text(
        f'{SIGNER_PRINCIPAL} namespaces="{SIGNATURE_NAMESPACE}" '
        f"{old_tokens[0]} {old_tokens[1]} retired\n",
        encoding="utf-8",
    )
    active, allowed_payload = build_signer_rotation_material(
        Path(str(current_key) + ".pub"),
        trust_policy=pending_path,
        allowed_signers=old_allowed,
    )
    policy = tmp_path / "active.json"
    policy.write_text(json.dumps(active, indent=2) + "\n", encoding="utf-8")
    allowed = tmp_path / "active.allowed"
    allowed.write_text(allowed_payload, encoding="utf-8")
    return old_key, current_key, old_record, old_signature, allowed, policy


def test_signature_binds_exact_certificate_content(tmp_path):
    key, allowed, policy = _active_material(tmp_path)
    record = _certificate_record(key, policy)

    signature = sign_certificate(record, key, trust_policy=policy)

    verification = verify_certificate_signature(
        record, signature, allowed_signers=allowed, trust_policy=policy
    )
    assert verification["valid"] is True
    assert verification["signer_epoch"] == 2
    tampered = {**record, "value": 2}
    assert verify_certificate_signature(
        tampered, signature, allowed_signers=allowed, trust_policy=policy
    )["valid"] is False


def test_signature_rejects_untrusted_issuer(tmp_path):
    key, _allowed, policy = _active_material(tmp_path, "issuer")
    _other_key, other_allowed, _other_policy = _active_material(tmp_path, "other")
    record = _certificate_record(key, policy)
    signature = sign_certificate(record, key, trust_policy=policy)

    result = verify_certificate_signature(
        record, signature, allowed_signers=other_allowed, trust_policy=policy
    )

    assert result["valid"] is False
    assert any("signature_invalid" in issue for issue in result["issues"])


def test_signing_environment_report_checks_epoch_key_and_trust(tmp_path, monkeypatch):
    key, allowed, policy = _active_material(tmp_path)
    monkeypatch.setenv("POK_OFFICIAL_SIGNING_KEY", str(key))

    report = signing_environment_report(
        allowed_signers=allowed, trust_policy=policy
    )

    assert report["ok"] is True
    assert report["identity"]["signer_epoch"] == 2
    assert report["threat_model"] == {
        "scope": "formal-publication-authentication-for-an-independently-anchored-public-key",
        "same_uid_llm_resistance": False,
        "private_key_isolation": "not-provided-by-current-single-uid-host",
        "out_of_band_public_key_anchor": "required-for-independent-verification",
        "runtime_or_strength_authority": "none",
    }


@pytest.mark.asyncio
async def test_real_orchestrator_hook_is_not_claimed_as_same_uid_key_isolation(
    monkeypatch,
):
    import evolution_core
    from orchestrator_context import _make_bot_dir_guard_hook

    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    matchers = _make_bot_dir_guard_hook()["PreToolUse"]
    by_tool = {matcher.matcher: matcher.hooks[0] for matcher in matchers}

    assert "Read" not in by_tool
    result = await by_tool["Bash"](
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "ssh-keygen -Y sign -f ~/.config/pok/"
                    "official_certifier_ed25519_epoch2 "
                    "-n pok-official-cert-v4 -"
                )
            },
        },
        "same-uid-threat-audit",
        None,
    )
    decision = getattr(result, "hookSpecificOutput", None) or {}

    assert decision.get("permissionDecision") != "deny"
    assert signing.SIGNER_THREAT_MODEL["same_uid_llm_resistance"] is False


def test_signing_environment_report_fails_for_non_current_key(tmp_path, monkeypatch):
    _key_current, allowed, policy = _active_material(tmp_path, "issuer")
    other_key = _key(tmp_path, "other")
    monkeypatch.setenv("POK_OFFICIAL_SIGNING_KEY", str(other_key))

    report = signing_environment_report(
        allowed_signers=allowed, trust_policy=policy
    )

    assert report["ok"] is False
    assert any("not the current signer epoch" in issue for issue in report["issues"])


def test_signing_identity_rejects_mismatched_public_key(tmp_path):
    key, _allowed, policy = _active_material(tmp_path, "issuer")
    other_key = _key(tmp_path, "other")
    Path(str(key) + ".pub").write_bytes(Path(str(other_key) + ".pub").read_bytes())

    with pytest.raises(RuntimeError, match="private/public key mismatch"):
        signing_identity(key, trust_policy=policy)


def test_environment_cannot_replace_production_trust_roots(tmp_path, monkeypatch):
    attacker_key, attacker_allowed, attacker_policy = _active_material(tmp_path, "attacker")
    record = _certificate_record(attacker_key, attacker_policy)
    signature = sign_certificate(record, attacker_key, trust_policy=attacker_policy)
    monkeypatch.setenv("POK_OFFICIAL_SIGNING_KEY", str(attacker_key))
    monkeypatch.setenv("POK_OFFICIAL_ALLOWED_SIGNERS", str(attacker_allowed))
    monkeypatch.setenv("POK_OFFICIAL_SIGNER_TRUST_POLICY", str(attacker_policy))

    default_result = verify_certificate_signature(record, signature)
    explicit_test_result = verify_certificate_signature(
        record,
        signature,
        allowed_signers=attacker_allowed,
        trust_policy=attacker_policy,
    )

    assert signing.allowed_signers_path() == signing.DEFAULT_ALLOWED_SIGNERS
    assert signing.signer_trust_policy_path() == signing.DEFAULT_TRUST_POLICY
    assert default_result["valid"] is False
    assert explicit_test_result["valid"] is True
    assert signing_environment_report()["ok"] is False


def test_rotation_required_policy_stops_issuance(tmp_path):
    key = _key(tmp_path, "not-activated")
    pending = deepcopy(load_signer_trust_policy())
    pending["current_signer"] = {
        "epoch": 2,
        "state": "rotation-required",
        "key_fingerprint": None,
        "public_key_sha256": None,
    }
    pending["policy_digest"] = signing._policy_digest(pending)
    pending_path = tmp_path / "rotation-required-policy.json"
    pending_path.write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(signing.SignerTrustPolicyError, match="rotation is required"):
        signing_identity(key, trust_policy=pending_path)


def test_retired_signer_validates_only_exact_record_and_signature(tmp_path):
    (
        old_key,
        current_key,
        old_record,
        old_signature,
        allowed,
        policy,
    ) = _historical_exception_material(tmp_path)

    exact = verify_certificate_signature(
        old_record, old_signature, allowed_signers=allowed, trust_policy=policy
    )
    new_old_record = {**old_record, "sequence": 2}
    new_old_signature = _raw_sign(new_old_record, old_key)
    tampered = verify_certificate_signature(
        new_old_record,
        new_old_signature,
        allowed_signers=allowed,
        trust_policy=policy,
    )
    current_record = bind_current_signer(
        {"schema_version": 2, "kind": "official-certificate-signing-readiness"},
        current_key,
        trust_policy=policy,
    )

    assert exact["valid"] is True
    assert exact["signer_state"] == "historical-validation-only"
    assert tampered["valid"] is False
    assert any("outside_exact_exception" in issue for issue in tampered["issues"])
    with pytest.raises(signing.SignerTrustPolicyError, match="not the current signer epoch"):
        sign_certificate(current_record, old_key, trust_policy=policy)


def test_missing_or_mismatched_policy_fails_closed(tmp_path):
    key, allowed, policy = _active_material(tmp_path)
    record = _certificate_record(key, policy)
    signature = sign_certificate(record, key, trust_policy=policy)
    missing = tmp_path / "missing-policy.json"
    broken = tmp_path / "broken-policy.json"
    payload = json.loads(policy.read_text(encoding="utf-8"))
    payload["current_epoch"] = 99
    broken.write_text(json.dumps(payload), encoding="utf-8")

    for policy_path in (missing, broken):
        result = verify_certificate_signature(
            record,
            signature,
            allowed_signers=allowed,
            trust_policy=policy_path,
        )
        assert result["valid"] is False
        assert any("trust_policy_invalid" in issue for issue in result["issues"])


def test_rotation_renderer_never_touches_private_key(tmp_path):
    key = _key(tmp_path, "epoch-2")
    private_before = key.stat()
    output = tmp_path / "review"
    pending = deepcopy(load_signer_trust_policy())
    pending["current_signer"] = {
        "epoch": pending["current_epoch"],
        "state": "rotation-required",
        "key_fingerprint": None,
        "public_key_sha256": None,
    }
    pending["policy_digest"] = signing._policy_digest(pending)
    pending_path = tmp_path / "renderer-pending.json"
    pending_path.write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")

    result = render_signer_rotation(
        Path(str(key) + ".pub"), output, trust_policy=pending_path
    )

    private_after = key.stat()
    assert result["private_key_touched"] is False
    assert result["current_epoch"] == 2
    assert private_after.st_mtime_ns == private_before.st_mtime_ns
    activated = load_signer_trust_policy(result["policy_output"])
    assert activated["current_signer"]["state"] == "active"
    assert "epoch-2" in Path(result["allowed_signers_output"]).read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        render_signer_rotation(
            Path(str(key) + ".pub"), output, trust_policy=pending_path
        )


def test_production_historical_validation_is_exact_and_non_issuing():
    policy = load_signer_trust_policy()
    historical = policy["historical_signers"]

    assert policy["current_epoch"] == 2
    assert policy["current_signer"] == {
        "epoch": 2,
        "state": "active",
        "key_fingerprint": "SHA256:93sCwWhJf1/y3HGhZOaOdHmiZHfb1VjMgg5jVZ/2urQ",
        "public_key_sha256": "196ebd37a4a365021c2bce2f3cada30f3e8bf19630a72aa57e03da3f310a9a54",
    }
    assert policy["policy_digest"] == (
        "5c4fbf0f1418a66162e2ca85a02833992328fe70d401b66c49d4c251d85faf15"
    )
    assert len(historical) == 1
    assert historical[0]["key_fingerprint"] == (
        "SHA256:2BvV/UxD+ma972mQLiLVLZbrRS9gk6LH3kpx0xCvlKU"
    )
    chain = historical[0]["historical_chain"]
    assert chain["candidate_label"] == "national_v141"
    assert chain["ledger_sequence"] == 1
    assert chain["bootstrap_root_id"] == (
        "national-v141-official-full-v5-signed-ledger-root"
    )
    assert historical[0]["state"] == "historical-validation-only"
    assert {item["purpose"] for item in historical[0]["allowed_records"]} == {
        "certificate",
        "ledger-entry",
        "ledger-head",
    }
