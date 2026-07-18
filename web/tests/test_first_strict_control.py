from __future__ import annotations

from copy import deepcopy


def _fresh_checkpoint() -> dict:
    from system_strict_bootstrap import build_fresh_bootstrap_receipt

    receipt = build_fresh_bootstrap_receipt(
        active_bots=(), epoch_reset_receipt_digest="a" * 64
    )
    return {
        "source_v": 142,
        "next_v": 143,
        "audit_context": {
            "protocol_bootstrap": receipt,
            "selection": {
                "strategy": "fresh_policy_bootstrap",
                "bootstrap_without_strength_evidence": True,
            },
        },
    }


def test_control_is_a_direct_content_bound_policy_artifact():
    import first_strict_control as control
    from bot_artifact import hash_path

    assert control.validate_control_package() == []
    path = control.materialize_control()
    assert control.validate_materialized_control(path) == []
    assert hash_path(path) == control.load_control_manifest()["expected_artifact_hash"]
    assert {item.name for item in path.iterdir()} == {
        "national_bot.py",
        "policy.py",
        "precompute.py",
        "national_runtime_manifest.json",
        "policy_epoch_receipt.json",
    }


def test_control_fails_closed_when_runtime_template_binding_drifts():
    import first_strict_control as control

    manifest = deepcopy(control.load_control_manifest())
    manifest["runtime"]["national_bot_sha256"] = "0" * 64

    assert "first_strict_control_national_runtime_hash_mismatch" in (
        control.validate_control_package(manifest)
    )


def test_control_receipt_is_empty_pool_only_and_never_strength_authority():
    import first_strict_control as control

    checkpoint = _fresh_checkpoint()
    receipt = control.build_control_receipt(checkpoint, active_bots=[])
    assert control.validate_control_receipt(
        receipt,
        checkpoint=checkpoint,
        active_bots=[],
    ) == []
    assert receipt["active_policy_bots"] == []
    assert receipt["formal_bootstrap_opponent_admitted"] is True
    assert receipt["formal_bootstrap_scope"] == "first_policy_bot_empty_pool_only"
    assert receipt["precommit_gate_admitted"] is True
    assert receipt["strength_admitted"] is False
    assert receipt["rating_eligible"] is False
    assert receipt["official_opponent_eligible"] is False


def test_control_receipt_rejects_pool_or_authority_escalation():
    import first_strict_control as control

    checkpoint = _fresh_checkpoint()
    receipt = control.build_control_receipt(checkpoint, active_bots=[])
    assert control.validate_control_receipt(
        receipt,
        checkpoint=checkpoint,
        active_bots=["national_v143"],
    )

    tampered = deepcopy(receipt)
    tampered["official_opponent_eligible"] = True
    assert any(
        "official_opponent_eligible" in issue
        for issue in control.validate_control_receipt(
            tampered,
            checkpoint=checkpoint,
            active_bots=[],
        )
    )


def test_control_opponent_projection_preserves_formal_only_scope():
    import first_strict_control as control

    receipt = control.build_control_receipt(_fresh_checkpoint(), active_bots=[])
    opponent = control.opponent_from_receipt(receipt)
    assert opponent["formal_bootstrap_opponent_admitted"] is True
    assert opponent["formal_bootstrap_scope"] == "first_policy_bot_empty_pool_only"
    assert opponent["strength_admitted"] is False
    assert opponent["rating_eligible"] is False
    assert opponent["official_opponent_eligible"] is False


def test_control_result_rejects_missing_zero_migration_projection():
    import first_strict_control as control

    issues, _summary = control.validate_control_result({"matchups": [{}]})
    assert "first_strict_control_matchup_migration_projection_mismatch" in issues
