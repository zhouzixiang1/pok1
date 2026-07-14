"""Depth-cutoff dispatch that has no private-state scalar leaf interface."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..cfv.semantics import RangeCFVQuery, RangeCFVResult
from .range_cfv_contract import RangeCFVLeafContract


class PrimaryLeafUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LeafUseReceipt:
    query_sha256: str
    result_sha256: str
    selected_provider_id: str
    selected_contract_sha256: str
    model_used: bool
    fallback_used: bool
    primary_invalid: bool
    primary_error_sha256: str | None


@dataclass(frozen=True, slots=True)
class LeafEvaluation:
    result: RangeCFVResult
    receipt: LeafUseReceipt


@dataclass(frozen=True, slots=True)
class PublicRangeLeafConsumer:
    """Use a primary full-range leaf or an explicit exact fallback."""

    primary: RangeCFVLeafContract | None
    fallback: RangeCFVLeafContract
    formal_require_primary: bool = False
    expected_primary_contract_sha256: str | None = None
    formal_require_neural_model: bool = False

    def __post_init__(self) -> None:
        if self.primary is not None and type(self.primary) is not RangeCFVLeafContract:
            raise TypeError("primary leaf must be a sealed RangeCFVLeafContract")
        if type(self.fallback) is not RangeCFVLeafContract:
            raise TypeError("fallback leaf must be a sealed RangeCFVLeafContract")
        if type(self.formal_require_primary) is not bool:
            raise TypeError("formal primary requirement must be an exact bool")
        if type(self.formal_require_neural_model) is not bool:
            raise TypeError("formal neural requirement must be an exact bool")
        expected = self.expected_primary_contract_sha256
        if expected is not None and (
            type(expected) is not str
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            raise ValueError("expected primary contract must be lowercase SHA-256")
        if self.formal_require_primary and expected is None:
            raise ValueError("formal primary mode requires an external expected digest")
        if self.formal_require_neural_model and not self.formal_require_primary:
            raise ValueError("formal neural mode requires formal primary mode")

    def evaluate(self, query: RangeCFVQuery) -> LeafEvaluation:
        if type(query) is not RangeCFVQuery:
            raise TypeError("public-range consumer requires exact RangeCFVQuery")
        primary_invalid = False
        error_digest: str | None = None
        if self.primary is not None:
            if (
                self.expected_primary_contract_sha256 is not None
                and self.primary.digest != self.expected_primary_contract_sha256
            ):
                if self.formal_require_primary:
                    raise PrimaryLeafUnavailable(
                        "primary leaf differs from the external expected contract digest"
                    )
                primary_invalid = True
                error_digest = hashlib.sha256(
                    b"primary-contract-digest-mismatch"
                ).hexdigest()
            elif self.formal_require_neural_model and self.primary.provider_kind != "neural_model":
                raise PrimaryLeafUnavailable(
                    "formal neural leaf requires a neural_model primary provider"
                )
            else:
                try:
                    result = self.primary.evaluate(query)
                except (TypeError, ValueError, ArithmeticError, RuntimeError) as exc:
                    primary_invalid = True
                    error_digest = hashlib.sha256(
                        f"{type(exc).__name__}:{exc}".encode("utf-8")
                    ).hexdigest()
                    if self.formal_require_primary:
                        raise PrimaryLeafUnavailable(
                            "formal public-range leaf rejected the primary provider"
                        ) from exc
                else:
                    if (
                        self.expected_primary_contract_sha256 is not None
                        and self.primary.digest
                        != self.expected_primary_contract_sha256
                    ):
                        raise PrimaryLeafUnavailable(
                            "primary leaf digest changed during evaluation"
                        )
                    if self.formal_require_neural_model and self.primary.provider_kind != "neural_model":
                        raise PrimaryLeafUnavailable(
                            "formal neural primary changed provider kind"
                        )
                    return LeafEvaluation(
                        result=result,
                        receipt=LeafUseReceipt(
                            query_sha256=query.digest,
                            result_sha256=result.digest,
                            selected_provider_id=self.primary.provider_id,
                            selected_contract_sha256=self.primary.digest,
                            model_used=self.primary.provider_kind == "neural_model",
                            fallback_used=False,
                            primary_invalid=False,
                            primary_error_sha256=None,
                        ),
                    )
        elif self.formal_require_primary:
            raise PrimaryLeafUnavailable("formal public-range leaf has no primary provider")

        result = self.fallback.evaluate(query)
        return LeafEvaluation(
            result=result,
            receipt=LeafUseReceipt(
                query_sha256=query.digest,
                result_sha256=result.digest,
                selected_provider_id=self.fallback.provider_id,
                selected_contract_sha256=self.fallback.digest,
                model_used=False,
                fallback_used=True,
                primary_invalid=primary_invalid,
                primary_error_sha256=error_digest,
            ),
        )
