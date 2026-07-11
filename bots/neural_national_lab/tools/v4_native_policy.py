"""Stdlib-only protected v4 policy for an authorized native candidate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from feature_spec import LABELS, label_action
from opponent_multitask_ensemble_runtime_v4 import (
    OpponentMultiTaskEnsembleRuntimeV4,
)
from v3_native_policy import (
    NativeV3Policy,
    _env_bool,
    _integer,
    _model_context,
    _strategy_features,
    candidate_actions,
    sanitize_stage_total,
)


BUNDLE_FILENAME = "v4_ensemble_bundle.json"
BUILD_MANIFEST_FILENAME = "V4_BUILD_MANIFEST.json"
BUILD_SCHEMA = "opponent_multitask_v4_native_candidate_build_v1"
GATE_RESULT_SCHEMA = "policy_gate_result_v3_win_first_v4"
EXPECTED_STRATEGY_DONOR_SHA256 = (
    "a8dadfefca945832df00a4bc438551834361f5464a8463dda20d146d02aa045d"
)
EXPECTED_STRATEGY_CRITICAL = {
    "national_bot.py": "60636a2fd03e4e570f716b56b6518bdeb7d9ceef44e2a8ebff6958c93dbf5be3",
    "strategy.py": "28a36e11f42aecd93dd931af01bc98241eea31fff64b262a36b120ca18bbcf7a",
    "neural_policy.py": "342cf69633ca87ec146f76d0523ec565e75be4d81251ed45bb1500da114e8a5c",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_bound_file(
    root: Path, name: Any, contract: Any, *, top_level: bool = False
) -> bool:
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(contract, dict)
        or set(contract) != {"bytes", "sha256"}
        or isinstance(contract["bytes"], bool)
        or not isinstance(contract["bytes"], int)
        or contract["bytes"] < 0
    ):
        return False
    relative = Path(name)
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or relative.as_posix() != name
    ):
        return False
    path = root
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            return False
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return False
    return bool(
        (not top_level or resolved.parent == root)
        and resolved.is_file()
        and resolved.stat().st_size == contract["bytes"]
        and _sha256(resolved) == contract["sha256"]
    )


def _authorized_bundle_path(bot_dir: Path) -> Path | None:
    try:
        root = bot_dir.resolve()
        manifest = json.loads(
            (root / BUILD_MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        authorization = manifest["authorization"]
        artifacts = manifest["candidate_artifacts"]
        donor_files = manifest["strategy_donor_derived_files"]
        strategy_donor = manifest["strategy_donor"]
        critical = strategy_donor["critical_files"]
        bundle_contract = artifacts[BUNDLE_FILENAME]
        false_claims = (
            "deployment_policy_value",
            "strength_evidence",
            "native_strength_evidence",
            "official_exe_accepted",
            "deployment_eligible",
        )
        if (
            manifest.get("schema") != BUILD_SCHEMA
            or any(manifest.get(field) is not False for field in false_claims)
            or authorization.get("deployment_policy_value") is not False
            or authorization.get("strength_evidence") is not False
            or manifest.get("native_build_contract")
            != authorization.get("native_build_contract")
            or not isinstance(artifacts, dict)
            or not artifacts
            or not isinstance(donor_files, dict)
            or not donor_files
            or strategy_donor.get("sha256")
            != EXPECTED_STRATEGY_DONOR_SHA256
            or not isinstance(critical, dict)
            or set(critical) != set(EXPECTED_STRATEGY_CRITICAL)
            or any(
                not isinstance(critical.get(name), dict)
                or set(critical[name]) != {"bytes", "sha256"}
                or isinstance(critical[name]["bytes"], bool)
                or not isinstance(critical[name]["bytes"], int)
                or critical[name]["bytes"] < 1
                or critical[name]["sha256"] != digest
                for name, digest in EXPECTED_STRATEGY_CRITICAL.items()
            )
            or any(
                donor_files.get(name) != critical.get(name)
                for name in ("strategy.py", "neural_policy.py")
            )
            or bundle_contract
            != {
                "bytes": authorization.get("bundle_bytes"),
                "sha256": authorization.get("bundle_sha256"),
            }
        ):
            return None
        for name, contract in artifacts.items():
            if not _valid_bound_file(root, name, contract, top_level=True):
                return None
        for name, contract in donor_files.items():
            if not _valid_bound_file(root, name, contract):
                return None
        evidence = root / "evidence" / "offline_policy_gate"
        evidence_contracts = {
            "artifact_manifest.json": "gate_artifact_manifest_sha256",
            "policy_gate_evaluation.json": "gate_evaluation_sha256",
            "policy_gate_result.json": "gate_result_sha256",
            "policy_gate_report.json": "gate_report_sha256",
        }
        for name, field in evidence_contracts.items():
            if _sha256(evidence / name) != authorization.get(field):
                return None
        result = json.loads(
            (evidence / "policy_gate_result.json").read_text(encoding="utf-8")
        )
        if (
            result.get("schema") != GATE_RESULT_SCHEMA
            or result.get("passed") is not True
            or result.get("errors") != []
            or result.get("native_candidate_build_authorized") is not True
            or result.get("bundle_bytes") != authorization.get("bundle_bytes")
            or result.get("bundle_sha256") != authorization.get("bundle_sha256")
            or result.get("native_build_contract")
            != authorization.get("native_build_contract")
            or result.get("deployment_policy_value") is not False
            or result.get("strength_evidence") is not False
        ):
            return None
        return root / BUNDLE_FILENAME
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


class NativeV4Policy(NativeV3Policy):
    """Apply the frozen win-first policy with an exact sanitized-rule fallback."""

    def __init__(self, runtime: OpponentMultiTaskEnsembleRuntimeV4) -> None:
        if runtime.policy is None:
            raise ValueError("v4 native policy requires a selected policy")
        self.runtime = runtime
        self.disable_cross_hand = _env_bool("POK_V4_DISABLE_CROSS_HAND")
        self.last_decision: dict[str, Any] | None = None

    @classmethod
    def load(cls, path: str | Path) -> "NativeV4Policy | None":
        if _env_bool("POK_V4_DISABLE"):
            return None
        runtime = OpponentMultiTaskEnsembleRuntimeV4.load(path)
        if runtime is None or runtime.policy is None:
            return None
        try:
            return cls(runtime)
        except Exception:
            return None

    def advise(
        self,
        request: dict[str, Any],
        state: dict[str, Any],
        safe_rule_action: int,
        strategy_context: dict[str, Any] | None,
    ) -> int:
        """Return an eligible v4 override or the same sanitized rule action.

        Every model, context, response, scoring, or post-selection validation
        failure is fail-closed.  In particular, a selected raise that changes
        label while being sanitized is not executed under another prediction.
        """
        # The caller passes v140's observed final baseline after its one and
        # only legacy sanitize_action call.  Never reinterpret that raise-to
        # total here: doing so would diverge from the collector.
        safe_rule = int(safe_rule_action)
        self.last_decision = {"used": False, "rule_action": safe_rule}
        try:
            alternatives = candidate_actions(request, state, safe_rule)
            if not alternatives:
                return safe_rule
            rule_label = int(label_action(safe_rule, request, None))
            legal_mask = [0] * len(LABELS)
            legal_mask[rule_label] = 1
            for candidate in alternatives:
                label_id = int(candidate["label_id"])
                if not 0 <= label_id < len(LABELS):
                    raise ValueError("v4 candidate label is out of range")
                legal_mask[label_id] = 1

            inputs = _model_context(
                request,
                state,
                legal_mask,
                response=False,
                disable_cross_hand=self.disable_cross_hand,
            )
            value_inputs = {
                **inputs,
                "rule_action": [
                    1.0 if index == rule_label else 0.0
                    for index in range(len(LABELS))
                ],
                "strategy_context": _strategy_features(None),
            }
            values = self.runtime.predict_values(**value_inputs)
            outcomes = self.runtime.predict_match_outcomes(**value_inputs)

            if float(self.runtime.policy["response_weight"]) > 0.0:
                for candidate in alternatives:
                    candidate["response_signal"] = self._response_signal(
                        request, state, legal_mask, candidate
                    )
            else:
                for candidate in alternatives:
                    candidate["response_signal"] = 0.0

            selected = self.runtime.select_candidate(
                values,
                outcomes,
                alternatives,
                rule_label_id=rule_label,
            )
            if selected is None:
                return safe_rule
            selected_action = int(selected["action"])
            selected_label = int(selected["label_id"])
            if not any(
                int(candidate["action"]) == selected_action
                and int(candidate["label_id"]) == selected_label
                for candidate in alternatives
            ):
                raise ValueError("v4 selector returned an unknown candidate")

            final = sanitize_stage_total(
                selected_action,
                state,
                _integer(request.get("my_chips")),
                fallback=safe_rule,
            )
            if (
                final != selected_action
                or int(label_action(final, request, None)) != selected_label
            ):
                self.last_decision = {
                    "used": False,
                    "rule_action": safe_rule,
                    "error": "selected action changed during final sanitization",
                }
                return safe_rule

            self.last_decision = {
                "used": final != safe_rule,
                "rule_action": safe_rule,
                "final_action": final,
                "label": LABELS[selected_label],
                "prediction": selected.get("prediction"),
                "response_signal": selected.get("response_signal", 0.0),
                "disable_cross_hand": self.disable_cross_hand,
            }
            return final
        except Exception as exc:
            self.last_decision = {
                "used": False,
                "rule_action": safe_rule,
                "error": f"{type(exc).__name__}: {exc}",
            }
            return safe_rule


def load_native_v4_policy(bot_dir: str | Path) -> NativeV4Policy | None:
    bundle = _authorized_bundle_path(Path(bot_dir))
    return NativeV4Policy.load(bundle) if bundle is not None else None
