"""Fail-closed, strategy-preserving mechanical policy repair contract.

The validator in this module deliberately operates on bytes and returns bytes.
It never receives an artifact path and never writes an artifact in place.  A
caller must materialize the returned policy into a new content-addressed
artifact revision and use the receipt to bind that output.

Only lexical changes to ``policy.py`` are eligible: comments, blank lines,
formatting, line endings, or source-encoding normalization.  The complete
Python AST remains authoritative, including imports, helpers, control flow,
type comments, constants, and docstrings.  The two system-owned executable
files must remain byte-for-byte identical.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import io
import json
import platform
import re
import sys
import tokenize
from typing import Any


POLICY_SEMANTIC_SCHEMA = "policy-semantic-digest-v1"
POLICY_SEMANTIC_DETECTOR = "national-policy-ast-semantic-detector"
POLICY_SEMANTIC_DETECTOR_VERSION = 1
MECHANICAL_REPAIR_RECEIPT_SCHEMA = "mechanical-policy-repair-receipt-v1"
AST_DUMP_CONTRACT = (
    "ast.parse(type_comments=True,feature_version=current);"
    "ast.dump(annotate_fields=True,include_attributes=False)"
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SEMANTIC_IDENTITY_FIELDS = frozenset({
    "schema",
    "detector",
    "detector_version",
    "python_implementation",
    "python_version",
    "python_cache_tag",
    "grammar",
    "feature_version",
    "ast_dump_contract",
    "ast_sha256",
    "detector_identity_digest",
    "semantic_digest",
})
_DETECTOR_IDENTITY_FIELDS = (
    "schema",
    "detector",
    "detector_version",
    "python_implementation",
    "python_version",
    "python_cache_tag",
    "grammar",
    "feature_version",
    "ast_dump_contract",
)
_REPAIR_RECEIPT_FIELDS = frozenset({
    "schema",
    "kind",
    "in_place_mutation_authorized",
    "allowed_changes",
    "policy_semantic_identity",
    "input",
    "output",
    "receipt_digest",
})
_REPAIR_SIDE_FIELDS = frozenset({
    "policy_sha256",
    "policy_size_bytes",
    "policy_encoding",
    "national_bot_sha256",
    "national_bot_size_bytes",
    "precompute_sha256",
    "precompute_size_bytes",
})
_ARTIFACT_MEMBER_NAMES = frozenset({
    "national_bot.py",
    "policy.py",
    "precompute.py",
})
_ALLOWED_CHANGES = [
    "comments",
    "blank_lines",
    "formatting",
    "line_endings",
    "source_encoding",
]


class MechanicalRepairRejected(ValueError):
    """The proposed output has no mechanical-repair authority."""

    def __init__(self, errors: list[str] | tuple[str, ...]):
        self.errors = tuple(str(error) for error in errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class PolicySemanticDigestV1:
    """Interpreter-bound semantic identity for one parseable policy source."""

    schema: str
    detector: str
    detector_version: int
    python_implementation: str
    python_version: str
    python_cache_tag: str
    grammar: str
    feature_version: tuple[int, int]
    ast_dump_contract: str
    ast_sha256: str
    detector_identity_digest: str
    semantic_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "detector": self.detector,
            "detector_version": self.detector_version,
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "python_cache_tag": self.python_cache_tag,
            "grammar": self.grammar,
            "feature_version": list(self.feature_version),
            "ast_dump_contract": self.ast_dump_contract,
            "ast_sha256": self.ast_sha256,
            "detector_identity_digest": self.detector_identity_digest,
            "semantic_digest": self.semantic_digest,
        }


@dataclass(frozen=True)
class MechanicalRepairOutput:
    """Immutable proposed bytes plus their content-bound validation receipt."""

    policy_bytes: bytes
    receipt: dict[str, Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_ok(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _detector_identity_digest(identity: dict[str, Any]) -> str:
    return _digest_bytes(_canonical_json({
        key: identity.get(key) for key in _DETECTOR_IDENTITY_FIELDS
    }).encode("utf-8"))


def _decode_python_source(source: bytes, *, subject: str) -> tuple[str, str]:
    if not isinstance(source, bytes):
        raise TypeError(f"{subject} must be bytes")
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
        return source.decode(encoding), encoding.lower()
    except (LookupError, SyntaxError, UnicodeDecodeError) as exc:
        raise MechanicalRepairRejected([
            f"{subject}_source_unparseable_no_repair_authority:"
            f"{type(exc).__name__}"
        ]) from exc


def _semantic_analysis(
    source: bytes,
    *,
    subject: str,
) -> tuple[PolicySemanticDigestV1, str]:
    text, encoding = _decode_python_source(source, subject=subject)
    feature_version = (sys.version_info.major, sys.version_info.minor)
    try:
        tree = ast.parse(
            text,
            filename="policy.py",
            mode="exec",
            type_comments=True,
            feature_version=feature_version,
        )
    except (SyntaxError, ValueError) as exc:
        raise MechanicalRepairRejected([
            f"{subject}_ast_unparseable_no_repair_authority:"
            f"{type(exc).__name__}"
        ]) from exc

    dumped = ast.dump(
        tree,
        annotate_fields=True,
        include_attributes=False,
    )
    ast_sha256 = _digest_bytes(dumped.encode("utf-8"))
    cache_tag = str(sys.implementation.cache_tag or "")
    if not cache_tag:
        raise MechanicalRepairRejected([
            "python_cache_tag_missing_no_repair_authority"
        ])
    identity = {
        "schema": POLICY_SEMANTIC_SCHEMA,
        "detector": POLICY_SEMANTIC_DETECTOR,
        "detector_version": POLICY_SEMANTIC_DETECTOR_VERSION,
        "python_implementation": str(sys.implementation.name),
        "python_version": platform.python_version(),
        "python_cache_tag": cache_tag,
        "grammar": (
            f"{sys.implementation.name}-python-ast-"
            f"{feature_version[0]}.{feature_version[1]}"
        ),
        "feature_version": list(feature_version),
        "ast_dump_contract": AST_DUMP_CONTRACT,
        "ast_sha256": ast_sha256,
    }
    identity["detector_identity_digest"] = _detector_identity_digest(identity)
    semantic_digest = _digest_bytes(_canonical_json(identity).encode("utf-8"))
    return PolicySemanticDigestV1(
        schema=str(identity["schema"]),
        detector=str(identity["detector"]),
        detector_version=int(identity["detector_version"]),
        python_implementation=str(identity["python_implementation"]),
        python_version=str(identity["python_version"]),
        python_cache_tag=str(identity["python_cache_tag"]),
        grammar=str(identity["grammar"]),
        feature_version=feature_version,
        ast_dump_contract=str(identity["ast_dump_contract"]),
        ast_sha256=ast_sha256,
        detector_identity_digest=str(identity["detector_identity_digest"]),
        semantic_digest=semantic_digest,
    ), encoding


def validate_mechanical_repair_receipt(receipt: Any) -> dict[str, Any]:
    """Parse one content-bound repair receipt without trusting its producer."""

    errors: list[str] = []
    if not isinstance(receipt, dict) or set(receipt) != _REPAIR_RECEIPT_FIELDS:
        raise MechanicalRepairRejected(["mechanical_repair_receipt_fields_invalid"])
    try:
        frozen = json.loads(_canonical_json(receipt))
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise MechanicalRepairRejected([
            "mechanical_repair_receipt_not_canonical_json"
        ]) from exc
    if frozen.get("schema") != MECHANICAL_REPAIR_RECEIPT_SCHEMA:
        errors.append("mechanical_repair_receipt_schema_invalid")
    if frozen.get("kind") != "policy-lexical-only-new-artifact-revision":
        errors.append("mechanical_repair_receipt_kind_invalid")
    if frozen.get("in_place_mutation_authorized") is not False:
        errors.append("mechanical_repair_in_place_authority_forbidden")
    if frozen.get("allowed_changes") != _ALLOWED_CHANGES:
        errors.append("mechanical_repair_allowed_changes_invalid")

    identity = frozen.get("policy_semantic_identity")
    if not isinstance(identity, dict) or set(identity) != _SEMANTIC_IDENTITY_FIELDS:
        errors.append("mechanical_repair_semantic_identity_fields_invalid")
    else:
        if (
            identity.get("schema") != POLICY_SEMANTIC_SCHEMA
            or identity.get("detector") != POLICY_SEMANTIC_DETECTOR
            or identity.get("detector_version") != POLICY_SEMANTIC_DETECTOR_VERSION
            or identity.get("ast_dump_contract") != AST_DUMP_CONTRACT
        ):
            errors.append("mechanical_repair_detector_identity_invalid")
        if not _digest_ok(identity.get("ast_sha256")):
            errors.append("mechanical_repair_ast_digest_invalid")
        expected_detector = _detector_identity_digest(identity)
        if identity.get("detector_identity_digest") != expected_detector:
            errors.append("mechanical_repair_detector_identity_digest_mismatch")
        semantic_body = {
            key: value
            for key, value in identity.items()
            if key != "semantic_digest"
        }
        expected_semantic = _digest_bytes(
            _canonical_json(semantic_body).encode("utf-8")
        )
        if identity.get("semantic_digest") != expected_semantic:
            errors.append("mechanical_repair_semantic_digest_mismatch")

    sides: dict[str, dict[str, Any]] = {}
    for side_name in ("input", "output"):
        side = frozen.get(side_name)
        if not isinstance(side, dict) or set(side) != _REPAIR_SIDE_FIELDS:
            errors.append(f"mechanical_repair_{side_name}_fields_invalid")
            continue
        sides[side_name] = side
        for field in (
            "policy_sha256",
            "national_bot_sha256",
            "precompute_sha256",
        ):
            if not _digest_ok(side.get(field)):
                errors.append(f"mechanical_repair_{side_name}_{field}_invalid")
        for member in ("policy", "national_bot", "precompute"):
            size = side.get(f"{member}_size_bytes")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                errors.append(
                    f"mechanical_repair_{side_name}_{member}_size_invalid"
                )
        encoding = side.get("policy_encoding")
        if not isinstance(encoding, str) or not encoding or len(encoding) > 128:
            errors.append(f"mechanical_repair_{side_name}_encoding_invalid")
    if set(sides) == {"input", "output"}:
        if sides["input"]["policy_sha256"] == sides["output"]["policy_sha256"]:
            errors.append("mechanical_repair_policy_output_unchanged")
        for field in ("national_bot_sha256", "precompute_sha256"):
            if sides["input"][field] != sides["output"][field]:
                errors.append(f"mechanical_repair_{field}_changed")

    unsigned = {key: value for key, value in frozen.items() if key != "receipt_digest"}
    if frozen.get("receipt_digest") != _digest_bytes(
        _canonical_json(unsigned).encode("utf-8")
    ):
        errors.append("mechanical_repair_receipt_digest_mismatch")
    if errors:
        raise MechanicalRepairRejected(errors)
    return frozen


def validate_mechanical_repair_receipt_against_artifact_bytes(
    receipt: Any,
    *,
    input_members: dict[str, bytes],
    output_members: dict[str, bytes],
) -> dict[str, Any]:
    """Recompute a receipt's authority from both exact three-member artifacts.

    A structurally self-consistent receipt is only a claim: an untrusted caller
    can replace system-member hashes or rewrite the AST identity and re-sign the
    JSON digest.  Admission must therefore resolve all three exact executable
    members for both artifacts, hash every byte string again, prove the two
    system-owned members are byte-identical, and compare both policy ASTs with
    the complete interpreter-bound semantic projection.  This helper performs
    that second, independent check; it never opens a path itself.
    """

    for side_name, members in (
        ("input", input_members),
        ("output", output_members),
    ):
        if not isinstance(members, dict) or set(members) != _ARTIFACT_MEMBER_NAMES:
            raise MechanicalRepairRejected([
                f"mechanical_repair_{side_name}_artifact_members_invalid"
            ])
        if any(not isinstance(value, bytes) for value in members.values()):
            raise TypeError("resolved mechanical repair artifact members must be bytes")
    frozen = validate_mechanical_repair_receipt(receipt)
    errors: list[str] = []
    resolved = {
        "input": input_members,
        "output": output_members,
    }
    identities: dict[str, PolicySemanticDigestV1] = {}
    for side_name, members in resolved.items():
        side = frozen[side_name]
        for member_path, receipt_prefix in (
            ("national_bot.py", "national_bot"),
            ("policy.py", "policy"),
            ("precompute.py", "precompute"),
        ):
            source = members[member_path]
            if _digest_bytes(source) != side[f"{receipt_prefix}_sha256"]:
                errors.append(
                    f"mechanical_repair_{side_name}_{receipt_prefix}_bytes_digest_mismatch"
                )
            if len(source) != side[f"{receipt_prefix}_size_bytes"]:
                errors.append(
                    f"mechanical_repair_{side_name}_{receipt_prefix}_bytes_size_mismatch"
                )
        policy_source = members["policy.py"]
        try:
            identity, encoding = _semantic_analysis(
                policy_source,
                subject=f"resolved_{side_name}_policy",
            )
        except MechanicalRepairRejected as exc:
            errors.extend(exc.errors)
            continue
        identities[side_name] = identity
        if encoding != side["policy_encoding"]:
            errors.append(f"mechanical_repair_{side_name}_policy_encoding_mismatch")
        if identity.as_dict() != frozen["policy_semantic_identity"]:
            errors.append(f"mechanical_repair_{side_name}_semantic_identity_mismatch")
    if set(identities) == {"input", "output"} and (
        identities["input"].as_dict() != identities["output"].as_dict()
    ):
        errors.append("mechanical_repair_resolved_policy_semantics_changed")
    for member_path, label in (
        ("national_bot.py", "national_bot"),
        ("precompute.py", "precompute"),
    ):
        if input_members[member_path] != output_members[member_path]:
            errors.append(f"mechanical_repair_resolved_{label}_bytes_changed")
    if errors:
        raise MechanicalRepairRejected(errors)
    return frozen


def policy_semantic_digest(source: bytes) -> PolicySemanticDigestV1:
    """Return the fail-closed semantic digest for parseable ``policy.py`` bytes."""

    identity, _encoding = _semantic_analysis(source, subject="policy")
    return identity


def build_mechanical_repair_output(
    *,
    input_policy: bytes,
    proposed_policy: bytes,
    input_national_bot: bytes,
    proposed_national_bot: bytes,
    input_precompute: bytes,
    proposed_precompute: bytes,
) -> MechanicalRepairOutput:
    """Validate and bind one lexical-only policy repair proposal.

    No filesystem operation occurs here.  Successful output is suitable only
    for a *new* artifact revision; this function does not authorize mutating an
    existing immutable artifact or reusing validation results from its old
    content hash.
    """

    byte_inputs = {
        "input_policy": input_policy,
        "proposed_policy": proposed_policy,
        "input_national_bot": input_national_bot,
        "proposed_national_bot": proposed_national_bot,
        "input_precompute": input_precompute,
        "proposed_precompute": proposed_precompute,
    }
    wrong_types = [
        name for name, value in byte_inputs.items() if not isinstance(value, bytes)
    ]
    if wrong_types:
        raise TypeError("mechanical repair inputs must be bytes: " + ",".join(wrong_types))

    errors: list[str] = []
    if input_national_bot != proposed_national_bot:
        errors.append("system_national_bot_bytes_changed")
    if input_precompute != proposed_precompute:
        errors.append("system_precompute_bytes_changed")
    if input_policy == proposed_policy:
        errors.append("policy_output_unchanged")
    if errors:
        raise MechanicalRepairRejected(errors)

    input_semantic, input_encoding = _semantic_analysis(
        input_policy,
        subject="input_policy",
    )
    output_semantic, output_encoding = _semantic_analysis(
        proposed_policy,
        subject="proposed_policy",
    )
    if input_semantic.semantic_digest != output_semantic.semantic_digest:
        raise MechanicalRepairRejected([
            "policy_semantic_digest_changed_strategy_repair_required"
        ])
    # Equality of the complete projection is stronger than comparing only its
    # terminal digest and catches an implementation that accidentally changes
    # a detector binding without updating the digest contract.
    if input_semantic.as_dict() != output_semantic.as_dict():
        raise MechanicalRepairRejected([
            "policy_semantic_identity_projection_mismatch"
        ])

    receipt_body = {
        "schema": MECHANICAL_REPAIR_RECEIPT_SCHEMA,
        "kind": "policy-lexical-only-new-artifact-revision",
        "in_place_mutation_authorized": False,
        "allowed_changes": list(_ALLOWED_CHANGES),
        "policy_semantic_identity": input_semantic.as_dict(),
        "input": {
            "policy_sha256": _digest_bytes(input_policy),
            "policy_size_bytes": len(input_policy),
            "policy_encoding": input_encoding,
            "national_bot_sha256": _digest_bytes(input_national_bot),
            "national_bot_size_bytes": len(input_national_bot),
            "precompute_sha256": _digest_bytes(input_precompute),
            "precompute_size_bytes": len(input_precompute),
        },
        "output": {
            "policy_sha256": _digest_bytes(proposed_policy),
            "policy_size_bytes": len(proposed_policy),
            "policy_encoding": output_encoding,
            "national_bot_sha256": _digest_bytes(proposed_national_bot),
            "national_bot_size_bytes": len(proposed_national_bot),
            "precompute_sha256": _digest_bytes(proposed_precompute),
            "precompute_size_bytes": len(proposed_precompute),
        },
    }
    receipt = {
        **receipt_body,
        "receipt_digest": _digest_bytes(
            _canonical_json(receipt_body).encode("utf-8")
        ),
    }
    validate_mechanical_repair_receipt(receipt)
    return MechanicalRepairOutput(
        policy_bytes=bytes(proposed_policy),
        receipt=receipt,
    )


__all__ = [
    "AST_DUMP_CONTRACT",
    "MECHANICAL_REPAIR_RECEIPT_SCHEMA",
    "MechanicalRepairOutput",
    "MechanicalRepairRejected",
    "POLICY_SEMANTIC_DETECTOR",
    "POLICY_SEMANTIC_DETECTOR_VERSION",
    "POLICY_SEMANTIC_SCHEMA",
    "PolicySemanticDigestV1",
    "build_mechanical_repair_output",
    "policy_semantic_digest",
    "validate_mechanical_repair_receipt",
    "validate_mechanical_repair_receipt_against_artifact_bytes",
]
