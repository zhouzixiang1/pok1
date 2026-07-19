from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from mechanical_repair import (
    MechanicalRepairRejected,
    build_mechanical_repair_output,
    policy_semantic_digest,
    validate_mechanical_repair_receipt,
    validate_mechanical_repair_receipt_against_artifact_bytes,
)


SYSTEM_BOT = b"SYSTEM_NATIVE_RUNTIME\n"
SYSTEM_PRECOMPUTE = b"SYSTEM_PRECOMPUTE\n"


def _build(before: bytes, after: bytes, **overrides):
    values = {
        "input_policy": before,
        "proposed_policy": after,
        "input_national_bot": SYSTEM_BOT,
        "proposed_national_bot": SYSTEM_BOT,
        "input_precompute": SYSTEM_PRECOMPUTE,
        "proposed_precompute": SYSTEM_PRECOMPUTE,
    }
    values.update(overrides)
    return build_mechanical_repair_output(**values)


def _receipt_digest(receipt):
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            b"def get_baseline_decision(context):\n    return {'intent': 'pass'}\n",
            b"# lexical cleanup\n\ndef get_baseline_decision( context ):\n    # same decision\n    return { 'intent': 'pass' }\n",
        ),
        (
            b"def get_baseline_decision(context):\n    return {'intent': 'pass'}\n",
            b"def get_baseline_decision(context):\r\n    return {'intent': 'pass'}\r\n",
        ),
        (
            "# coding: latin-1\n# café\ndef get_baseline_decision(context):\n    return {'intent': 'pass'}\n".encode(
                "latin-1"
            ),
            "# coding: utf-8\n# café\ndef get_baseline_decision(context):\n    return {'intent': 'pass'}\n".encode(
                "utf-8"
            ),
        ),
    ],
)
def test_accepts_only_lexical_comment_format_line_ending_or_encoding_changes(
    before,
    after,
):
    output = _build(before, after)

    assert output.policy_bytes == after
    assert output.receipt["kind"] == "policy-lexical-only-new-artifact-revision"
    assert output.receipt["in_place_mutation_authorized"] is False
    assert output.receipt["input"]["policy_sha256"] == hashlib.sha256(before).hexdigest()
    assert output.receipt["output"]["policy_sha256"] == hashlib.sha256(after).hexdigest()
    assert output.receipt["receipt_digest"] == _receipt_digest(output.receipt)
    identity = output.receipt["policy_semantic_identity"]
    assert identity["semantic_digest"] == policy_semantic_digest(before).semantic_digest
    assert identity["detector_version"] == 1
    assert identity["python_cache_tag"]
    assert identity["grammar"]


@pytest.mark.parametrize(
    ("after", "reason"),
    [
        (
            b"LIMIT = 11\ndef decide(x):\n    return LIMIT\n",
            "constant",
        ),
        (
            b"LIMIT = 10\ndef decide(x):\n    return x <= LIMIT\n",
            "compare",
        ),
        (
            b"LIMIT = 10\ndef decide(x):\n    if x > LIMIT:\n        return 1\n    return 0\n",
            "control",
        ),
        (
            b"import math\nLIMIT = 10\ndef decide(x):\n    return x > LIMIT\n",
            "import",
        ),
        (
            b"LIMIT = 10\ndef helper():\n    return 1\ndef decide(x):\n    return x > LIMIT\n",
            "helper",
        ),
        (
            b'"changed module documentation"\nLIMIT = 10\ndef decide(x):\n    return x > LIMIT\n',
            "docstring",
        ),
    ],
)
def test_rejects_any_ast_semantic_change(after, reason):
    before = b'"module documentation"\nLIMIT = 10\ndef decide(x):\n    return x > LIMIT\n'

    with pytest.raises(MechanicalRepairRejected) as caught:
        _build(before, after)

    assert caught.value.errors == (
        "policy_semantic_digest_changed_strategy_repair_required",
    ), reason


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (
            b"def broken(:\n    pass\n",
            b"def broken():\n    pass\n",
            "input_policy_ast_unparseable_no_repair_authority:SyntaxError",
        ),
        (
            b"def valid():\n    pass\n",
            b"def broken(:\n    pass\n",
            "proposed_policy_ast_unparseable_no_repair_authority:SyntaxError",
        ),
    ],
)
def test_syntax_invalid_source_has_no_mechanical_repair_authority(
    before,
    after,
    expected,
):
    with pytest.raises(MechanicalRepairRejected) as caught:
        _build(before, after)

    assert caught.value.errors == (expected,)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"proposed_national_bot": SYSTEM_BOT + b"drift"},
            "system_national_bot_bytes_changed",
        ),
        (
            {"proposed_precompute": SYSTEM_PRECOMPUTE + b"drift"},
            "system_precompute_bytes_changed",
        ),
    ],
)
def test_rejects_any_system_owned_byte_change(overrides, expected):
    before = b"def decide(x):\n    return x\n"
    after = b"# formatting only\ndef decide( x ):\n    return x\n"

    with pytest.raises(MechanicalRepairRejected) as caught:
        _build(before, after, **overrides)

    assert expected in caught.value.errors


def test_rejects_noop_instead_of_manufacturing_a_repair_receipt():
    policy = b"def decide(x):\n    return x\n"

    with pytest.raises(MechanicalRepairRejected) as caught:
        _build(policy, policy)

    assert caught.value.errors == ("policy_output_unchanged",)


def test_receipt_binds_output_without_mutating_input_buffers():
    before = b"def decide(x):\n    return x\n"
    after = b"# comment\ndef decide(x):\n    return x\n"
    input_copy = bytes(before)

    output = _build(before, after)

    assert before == input_copy
    assert output.policy_bytes is not before
    assert output.policy_bytes == after
    assert output.receipt["output"]["national_bot_sha256"] == output.receipt["input"][
        "national_bot_sha256"
    ]
    assert output.receipt["output"]["precompute_sha256"] == output.receipt["input"][
        "precompute_sha256"
    ]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        (
            "detector_identity_digest",
            "mechanical_repair_detector_identity_digest_mismatch",
        ),
        ("semantic_digest", "mechanical_repair_semantic_digest_mismatch"),
    ],
)
def test_receipt_parser_rejects_resigned_detector_or_semantic_identity_drift(
    field,
    expected,
):
    output = _build(
        b"def decide(x):\n    return x\n",
        b"# lexical\ndef decide( x ):\n    return x\n",
    )
    receipt = deepcopy(output.receipt)
    receipt["policy_semantic_identity"][field] = "f" * 64
    receipt["receipt_digest"] = _receipt_digest(receipt)

    with pytest.raises(MechanicalRepairRejected) as caught:
        validate_mechanical_repair_receipt(receipt)

    assert expected in caught.value.errors


def test_resolved_policy_bytes_defeat_a_self_consistent_resigned_semantic_claim():
    before = b"def decide(context):\n    return {'intent': 'pass'}\n"
    lexical = b"# lexical\ndef decide(context):\n    return {'intent': 'pass'}\n"
    strategy_change = b"def decide(context):\n    return {'intent': 'fold'}\n"
    receipt = deepcopy(_build(before, lexical).receipt)

    # This is exactly the adversarial shape the JSON-only parser cannot detect:
    # replace the claimed AST identity and output member, then re-sign every
    # content digest.  The claim is self-consistent but false for the parent.
    receipt["policy_semantic_identity"] = policy_semantic_digest(
        strategy_change
    ).as_dict()
    receipt["output"]["policy_sha256"] = hashlib.sha256(
        strategy_change
    ).hexdigest()
    receipt["output"]["policy_size_bytes"] = len(strategy_change)
    receipt["receipt_digest"] = _receipt_digest(receipt)

    assert validate_mechanical_repair_receipt(receipt) == receipt
    with pytest.raises(MechanicalRepairRejected) as caught:
        validate_mechanical_repair_receipt_against_artifact_bytes(
            receipt,
            input_members={
                "national_bot.py": SYSTEM_BOT,
                "policy.py": before,
                "precompute.py": SYSTEM_PRECOMPUTE,
            },
            output_members={
                "national_bot.py": SYSTEM_BOT,
                "policy.py": strategy_change,
                "precompute.py": SYSTEM_PRECOMPUTE,
            },
        )

    assert "mechanical_repair_input_semantic_identity_mismatch" in caught.value.errors
    assert "mechanical_repair_resolved_policy_semantics_changed" in caught.value.errors


@pytest.mark.parametrize(
    ("receipt_field", "issue_fragment"),
    [
        ("national_bot_sha256", "national_bot_bytes_digest"),
        ("precompute_sha256", "precompute_bytes_digest"),
    ],
)
def test_resolved_system_members_defeat_equal_resigned_fake_hashes(
    receipt_field,
    issue_fragment,
):
    before = b"def decide(context):\n    return {'intent': 'pass'}\n"
    after = b"# lexical\ndef decide(context):\n    return {'intent': 'pass'}\n"
    receipt = deepcopy(_build(before, after).receipt)
    receipt["input"][receipt_field] = "f" * 64
    receipt["output"][receipt_field] = "f" * 64
    receipt["receipt_digest"] = _receipt_digest(receipt)

    assert validate_mechanical_repair_receipt(receipt) == receipt
    with pytest.raises(MechanicalRepairRejected) as caught:
        validate_mechanical_repair_receipt_against_artifact_bytes(
            receipt,
            input_members={
                "national_bot.py": SYSTEM_BOT,
                "policy.py": before,
                "precompute.py": SYSTEM_PRECOMPUTE,
            },
            output_members={
                "national_bot.py": SYSTEM_BOT,
                "policy.py": after,
                "precompute.py": SYSTEM_PRECOMPUTE,
            },
        )

    assert any(issue_fragment in issue for issue in caught.value.errors)
