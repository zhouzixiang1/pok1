"""Stdlib-only ensemble wrapper for catastrophe heads and base encoders."""
from __future__ import annotations

import hashlib
from pathlib import Path
import statistics
from typing import Any

from opp_catastrophe_runtime import OpponentCatastropheRuntime
from opp_multitask_runtime import OpponentMultiTaskRuntime


class OpponentCatastropheEnsemble:
    def __init__(
        self,
        members: list[
            tuple[OpponentMultiTaskRuntime, OpponentCatastropheRuntime]
        ],
        *,
        std_multiplier: float = 1.0,
    ) -> None:
        if not members:
            raise ValueError("catastrophe ensemble requires at least one member")
        self.members = list(members)
        self.std_multiplier = max(0.0, float(std_multiplier))
        self.labels = list(self.members[0][1].labels)
        for base, risk in self.members:
            if list(base.labels) != self.labels or risk.labels != self.labels:
                raise ValueError("catastrophe ensemble label mismatch")
            latent = int((base.meta.get("model") or {}).get("latent", 0))
            if latent != risk.latent_dim:
                raise ValueError("catastrophe ensemble latent mismatch")

    @classmethod
    def from_paths(
        cls,
        base_paths: list[str | Path],
        risk_paths: list[str | Path],
        *,
        std_multiplier: float = 1.0,
    ) -> "OpponentCatastropheEnsemble | None":
        if len(base_paths) != len(risk_paths) or not base_paths:
            return None
        pairs = []
        try:
            for base_path, risk_path in zip(base_paths, risk_paths):
                base_bytes = Path(base_path).read_bytes()
                base_sha256 = hashlib.sha256(base_bytes).hexdigest()
                base = OpponentMultiTaskRuntime.load(base_path)
                risk = OpponentCatastropheRuntime.load(
                    risk_path, expected_base_sha256=base_sha256
                )
                if base is None or risk is None:
                    return None
                pairs.append((base, risk))
            return cls(pairs, std_multiplier=std_multiplier)
        except Exception:
            return None

    def predict(
        self,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_hand: list[float],
        rule_label_id: int,
        cross_sequence: list[list[float]] | None = None,
    ) -> dict[str, Any]:
        predictions = []
        for base, risk in self.members:
            latent = base.encode(
                state,
                profile,
                history,
                cross_hand,
                rule_label_id,
                cross_sequence,
            )
            prediction = risk.predict(latent, rule_label_id)
            if not prediction:
                return {}
            predictions.append(prediction)

        result: dict[str, Any] = {"members": len(predictions)}
        for field in ("probability", "severity", "expected_loss"):
            mean = []
            std = []
            upper = []
            for action_id in range(len(self.labels)):
                values = [row[field][action_id] for row in predictions]
                item_mean = statistics.fmean(values)
                item_std = statistics.pstdev(values)
                item_upper = item_mean + self.std_multiplier * item_std
                if field == "probability":
                    item_upper = min(1.0, max(0.0, item_upper))
                mean.append(item_mean)
                std.append(item_std)
                upper.append(item_upper)
            result[field] = mean
            result[f"{field}_std"] = std
            result[f"{field}_upper"] = upper
        return result
