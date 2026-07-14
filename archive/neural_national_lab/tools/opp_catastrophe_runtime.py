"""Stdlib-only runtime for a frozen-latent catastrophe risk head."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from opp_value_runtime import _matvec, _relu, _sigmoid


class OpponentCatastropheRuntime:
    """Predict action-level catastrophic-loss probability and severity."""

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        expected_base_sha256: str | None = None,
    ) -> None:
        meta = payload.get("meta") or {}
        if meta.get("format") != "opp_catastrophe_head_v1":
            raise ValueError("unsupported catastrophe head format")
        self.meta = meta
        self.weights = payload.get("weights") or {}
        self.labels = list(meta.get("labels") or [])
        self.latent_dim = int(meta.get("latent_dim", 0))
        self.hidden = int((meta.get("model") or {}).get("hidden", 0))
        risk = meta.get("risk") or {}
        self.catastrophe_threshold = float(
            risk.get("catastrophe_threshold", 5000.0)
        )
        self.severity_clip = float(risk.get("severity_clip", 20000.0))
        self.base_model_sha256 = str(
            (meta.get("base_model") or {}).get("sha256", "")
        )
        calibration = risk.get("calibration") or {}
        self.calibration_scale = float(calibration.get("scale", 1.0))
        self.calibration_bias = float(calibration.get("bias", 0.0))
        if not self.labels or self.latent_dim <= 0 or self.hidden <= 0:
            raise ValueError("invalid catastrophe head dimensions")
        if self.severity_clip <= 0 or self.catastrophe_threshold <= 0:
            raise ValueError("invalid catastrophe risk thresholds")
        if expected_base_sha256 is not None and (
            self.base_model_sha256 != str(expected_base_sha256)
        ):
            raise ValueError("catastrophe head/base model hash mismatch")

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        expected_base_sha256: str | None = None,
    ) -> "OpponentCatastropheRuntime | None":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(payload, expected_base_sha256=expected_base_sha256)
        except Exception:
            return None

    @staticmethod
    def file_sha256(path: str | Path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def _head_forward(self, latent: list[float]) -> list[float]:
        if len(latent) != self.latent_dim:
            return []
        hidden = _relu(_matvec(
            self.weights.get("head.0.weight"),
            latent,
            self.weights.get("head.0.bias"),
        ))
        if len(hidden) != self.hidden:
            return []
        return _matvec(
            self.weights.get("head.2.weight"),
            hidden,
            self.weights.get("head.2.bias"),
        )

    def predict(
        self,
        latent: list[float],
        rule_label_id: int,
    ) -> dict[str, list[float]]:
        try:
            raw = self._head_forward(list(latent))
            action_count = len(self.labels)
            if len(raw) != action_count * 2:
                return {}
            probabilities = [
                _sigmoid(self.calibration_scale * value + self.calibration_bias)
                for value in raw[:action_count]
            ]
            severities = [
                self.severity_clip * _sigmoid(value)
                for value in raw[action_count:]
            ]
            expected_losses = [
                probability * severity
                for probability, severity in zip(probabilities, severities)
            ]
            safe_rule_id = max(0, min(action_count - 1, int(rule_label_id)))
            probabilities[safe_rule_id] = 0.0
            severities[safe_rule_id] = 0.0
            expected_losses[safe_rule_id] = 0.0
            return {
                "probability": probabilities,
                "severity": severities,
                "expected_loss": expected_losses,
            }
        except Exception:
            return {}
