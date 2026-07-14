"""Immutable query/result objects for public-range counterfactual values."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from bots.research_native_lab.cfr_neural_search.core.identity import payload_sha256

from .combo_index import COMBO_COUNT
from .public_state import PublicHUNLState
from .ranges import cfv_valid_mask, validate_reach_range


def _matrix(
    values: object,
    context: str,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if type(values) is not tuple or len(values) != 2:
        raise TypeError(f"{context} must be an immutable two-player matrix")
    rows: list[tuple[float, ...]] = []
    for row in values:
        if type(row) is not tuple or len(row) != COMBO_COUNT:
            raise TypeError(f"{context} rows must have length 1,326")
        normalized: list[float] = []
        for value in row:
            if type(value) not in (int, float):
                raise TypeError(f"{context} values must be exact JSON numbers")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"{context} values must be finite")
            normalized.append(number)
        rows.append(tuple(normalized))
    return rows[0], rows[1]


@dataclass(frozen=True, slots=True)
class RangeCFVQuery:
    public_state: PublicHUNLState
    private_ranges: tuple[tuple[float, ...], tuple[float, ...]]

    def __post_init__(self) -> None:
        if type(self.public_state) is not PublicHUNLState:
            raise TypeError("CFV query requires exact PublicHUNLState")
        if type(self.private_ranges) is not tuple or len(self.private_ranges) != 2:
            raise TypeError("CFV query requires two immutable private ranges")
        ranges = (
            validate_reach_range(
                self.private_ranges[0], self.public_state.board_card_ids
            ),
            validate_reach_range(
                self.private_ranges[1], self.public_state.board_card_ids
            ),
        )
        object.__setattr__(self, "private_ranges", ranges)
        self.assert_authoritative()

    def assert_authoritative(self) -> None:
        """Reprove the query's public state and ranges at each trust boundary."""

        if type(self.public_state) is not PublicHUNLState:
            raise TypeError("CFV query requires exact PublicHUNLState")
        self.public_state.replay_common_public_state()
        if type(self.private_ranges) is not tuple or len(self.private_ranges) != 2:
            raise TypeError("CFV query requires two immutable private ranges")
        observed = (
            validate_reach_range(
                self.private_ranges[0], self.public_state.board_card_ids
            ),
            validate_reach_range(
                self.private_ranges[1], self.public_state.board_card_ids
            ),
        )
        if payload_sha256({"ranges": observed}) != payload_sha256(
            {"ranges": self.private_ranges}
        ):
            raise ValueError("CFV query ranges differ from their canonical representation")

    @property
    def valid_masks(self) -> tuple[tuple[bool, ...], tuple[bool, ...]]:
        self.assert_authoritative()
        return (
            cfv_valid_mask(self.public_state.board_card_ids, self.private_ranges[1]),
            cfv_valid_mask(self.public_state.board_card_ids, self.private_ranges[0]),
        )

    def to_payload(self) -> dict[str, Any]:
        self.assert_authoritative()
        return {
            "schema": "route-b-m5-range-cfv-query-v1",
            "public_state": self.public_state.to_payload(),
            "private_ranges": [list(row) for row in self.private_ranges],
        }

    @property
    def digest(self) -> str:
        return payload_sha256(self.to_payload())


@dataclass(frozen=True, slots=True)
class RangeCFVResult:
    query_sha256: str
    provider_id: str
    raw_values: tuple[tuple[float, ...], tuple[float, ...]]
    values: tuple[tuple[float, ...], tuple[float, ...]]
    valid_masks: tuple[tuple[bool, ...], tuple[bool, ...]]
    raw_zero_sum_residual_bb: float
    deployed_zero_sum_residual_bb: float

    def __post_init__(self) -> None:
        if (
            type(self.query_sha256) is not str
            or len(self.query_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.query_sha256)
        ):
            raise ValueError("result query identity must be lowercase SHA-256")
        if type(self.provider_id) is not str or not self.provider_id:
            raise ValueError("CFV provider id must be a nonempty exact string")
        raw = _matrix(self.raw_values, "raw CFV")
        deployed = _matrix(self.values, "deployed CFV")
        if type(self.valid_masks) is not tuple or len(self.valid_masks) != 2:
            raise TypeError("CFV masks must be an immutable two-player matrix")
        masks: list[tuple[bool, ...]] = []
        for row in self.valid_masks:
            if type(row) is not tuple or len(row) != COMBO_COUNT or any(
                type(value) is not bool for value in row
            ):
                raise TypeError("CFV mask rows must contain 1,326 exact bools")
            masks.append(row)
        for player in (0, 1):
            for index, enabled in enumerate(masks[player]):
                if not enabled and (
                    raw[player][index] != 0.0 or deployed[player][index] != 0.0
                ):
                    raise ValueError("masked CFV outputs must be canonical zero")
        raw_residual = float(self.raw_zero_sum_residual_bb)
        deployed_residual = float(self.deployed_zero_sum_residual_bb)
        if not math.isfinite(raw_residual) or not math.isfinite(deployed_residual):
            raise ValueError("CFV zero-sum residuals must be finite")
        object.__setattr__(self, "raw_values", raw)
        object.__setattr__(self, "values", deployed)
        object.__setattr__(self, "valid_masks", (masks[0], masks[1]))
        object.__setattr__(self, "raw_zero_sum_residual_bb", raw_residual)
        object.__setattr__(self, "deployed_zero_sum_residual_bb", deployed_residual)

    @classmethod
    def create(
        cls,
        query: RangeCFVQuery,
        *,
        provider_id: str,
        raw_values: tuple[tuple[float, ...], tuple[float, ...]],
        deployed_values: tuple[tuple[float, ...], tuple[float, ...]] | None = None,
        enforce_exact_zero_sum: bool = False,
        exact_tolerance: float = 1e-10,
    ) -> "RangeCFVResult":
        if type(query) is not RangeCFVQuery:
            raise TypeError("CFV result factory requires exact query")
        raw = _matrix(raw_values, "raw CFV")
        deployed = raw if deployed_values is None else _matrix(deployed_values, "deployed CFV")
        masks = query.valid_masks
        canonical_raw = tuple(
            tuple(value if masks[player][index] else 0.0 for index, value in enumerate(raw[player]))
            for player in (0, 1)
        )
        canonical_deployed = tuple(
            tuple(
                value if masks[player][index] else 0.0
                for index, value in enumerate(deployed[player])
            )
            for player in (0, 1)
        )

        def residual(matrix: tuple[tuple[float, ...], tuple[float, ...]]) -> float:
            return math.fsum(
                query.private_ranges[player][index] * matrix[player][index]
                for player in (0, 1)
                for index in range(COMBO_COUNT)
            )

        raw_residual = residual(canonical_raw)  # type: ignore[arg-type]
        deployed_residual = residual(canonical_deployed)  # type: ignore[arg-type]
        if enforce_exact_zero_sum and (
            abs(raw_residual) > exact_tolerance
            or abs(deployed_residual) > exact_tolerance
        ):
            raise ValueError("exact CFV oracle violated range-weighted zero sum")
        return cls(
            query_sha256=query.digest,
            provider_id=provider_id,
            raw_values=canonical_raw,  # type: ignore[arg-type]
            values=canonical_deployed,  # type: ignore[arg-type]
            valid_masks=masks,
            raw_zero_sum_residual_bb=raw_residual,
            deployed_zero_sum_residual_bb=deployed_residual,
        )

    def validate_against(self, query: RangeCFVQuery, *, tolerance: float = 1e-12) -> None:
        if type(query) is not RangeCFVQuery:
            raise TypeError("result validation requires exact RangeCFVQuery")
        if self.query_sha256 != query.digest:
            raise ValueError("CFV result is bound to a different query")
        if self.valid_masks != query.valid_masks:
            raise ValueError("CFV result validity masks differ from blocker semantics")

        def residual(matrix: tuple[tuple[float, ...], tuple[float, ...]]) -> float:
            return math.fsum(
                query.private_ranges[player][index] * matrix[player][index]
                for player in (0, 1)
                for index in range(COMBO_COUNT)
            )

        raw = residual(self.raw_values)
        deployed = residual(self.values)
        if not math.isclose(
            raw,
            self.raw_zero_sum_residual_bb,
            rel_tol=0.0,
            abs_tol=tolerance,
        ) or not math.isclose(
            deployed,
            self.deployed_zero_sum_residual_bb,
            rel_tol=0.0,
            abs_tol=tolerance,
        ):
            raise ValueError("CFV result residual diagnostics do not match its vectors")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "route-b-m5-range-cfv-result-v1",
            "query_sha256": self.query_sha256,
            "provider_id": self.provider_id,
            "raw_values": [list(row) for row in self.raw_values],
            "values": [list(row) for row in self.values],
            "valid_masks": [list(row) for row in self.valid_masks],
            "raw_zero_sum_residual_bb": self.raw_zero_sum_residual_bb,
            "deployed_zero_sum_residual_bb": self.deployed_zero_sum_residual_bb,
        }

    @property
    def digest(self) -> str:
        return payload_sha256(self.to_payload())
