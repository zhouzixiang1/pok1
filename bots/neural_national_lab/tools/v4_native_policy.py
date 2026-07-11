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
from v4_runtime_budget import (
    MAX_BUNDLE_BYTES,
    bundle_runtime_identity_sha256,
    validate_runtime_budget_artifact,
)


BUNDLE_FILENAME = "v4_ensemble_bundle.json"
BUILD_MANIFEST_FILENAME = "V4_BUILD_MANIFEST.json"
RUNTIME_BUDGET_FILENAME = "V4_RUNTIME_BUDGET.json"
BUILD_SCHEMA = "opponent_multitask_v4_native_candidate_build_v1"
GATE_RESULT_SCHEMA = "policy_gate_result_v3_win_first_v4"
GATE_EVALUATION_SCHEMA = "opponent_multitask_v4_policy_gate_evaluation_v1"
GATE_REPORT_SCHEMA = "opponent_multitask_v4_policy_gate_report_v1"
GATE_ARTIFACT_SCHEMA = "opponent_multitask_v4_policy_gate_artifacts_v1"
OUTCOME_UNCERTAINTY_MATCH_ABLATION_ENV = (
    "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH"
)
ABLATION_MODE_FULL = "full"
ABLATION_MODE_CROSS_HAND_OFF = "cross_hand_off"
ABLATION_MODE_OUTCOME_UNCERTAINTY_MATCH_OFF = (
    "outcome_uncertainty_match_off"
)
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


def _file_snapshot(
    root: Path, name: Any, *, top_level: bool = False
) -> bytes | None:
    """Read one contained regular file exactly once after rejecting symlinks."""
    if (
        not isinstance(name, str)
        or not name
    ):
        return None
    relative = Path(name)
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or relative.as_posix() != name
    ):
        return None
    try:
        trusted_root = root.resolve(strict=True)
        path = trusted_root
        for part in relative.parts:
            path /= part
            if path.is_symlink():
                return None
        resolved = path.resolve(strict=True)
        resolved.relative_to(trusted_root)
        if (
            (top_level and resolved.parent != trusted_root)
            or not resolved.is_file()
        ):
            return None
        return resolved.read_bytes()
    except (OSError, RuntimeError, ValueError):
        return None


def _bound_file_snapshot(
    root: Path, name: Any, contract: Any, *, top_level: bool = False
) -> bytes | None:
    if (
        not isinstance(contract, dict)
        or set(contract) != {"bytes", "sha256"}
        or isinstance(contract.get("bytes"), bool)
        or not isinstance(contract.get("bytes"), int)
        or contract["bytes"] < 0
        or not isinstance(contract.get("sha256"), str)
    ):
        return None
    raw = _file_snapshot(root, name, top_level=top_level)
    if (
        raw is None
        or len(raw) != contract["bytes"]
        or hashlib.sha256(raw).hexdigest() != contract["sha256"]
    ):
        return None
    return raw


def _valid_bound_file(
    root: Path, name: Any, contract: Any, *, top_level: bool = False
) -> bool:
    """Compatibility wrapper for callers that need only a boolean result."""
    return _bound_file_snapshot(
        root, name, contract, top_level=top_level
    ) is not None


def _authorized_bundle_payload(bot_dir: Path) -> dict[str, Any] | None:
    try:
        root = bot_dir.resolve()
        manifest_raw = _file_snapshot(
            root, BUILD_MANIFEST_FILENAME, top_level=True
        )
        if manifest_raw is None:
            return None
        manifest = json.loads(manifest_raw)
        if not isinstance(manifest, dict):
            return None
        authorization = manifest.get("authorization")
        artifacts = manifest.get("candidate_artifacts")
        donor_files = manifest.get("strategy_donor_derived_files")
        strategy_donor = manifest.get("strategy_donor")
        if (
            not isinstance(authorization, dict)
            or not isinstance(artifacts, dict)
            or not artifacts
            or not isinstance(donor_files, dict)
            or not donor_files
            or not isinstance(strategy_donor, dict)
        ):
            return None
        critical = strategy_donor.get("critical_files")
        bundle_contract = artifacts.get(BUNDLE_FILENAME)
        runtime_budget_contract = artifacts.get(RUNTIME_BUDGET_FILENAME)
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
            or not isinstance(bundle_contract, dict)
            or bundle_contract
            != {
                "bytes": authorization.get("bundle_bytes"),
                "sha256": authorization.get("bundle_sha256"),
            }
            or bundle_contract["bytes"] > MAX_BUNDLE_BYTES
            or not isinstance(runtime_budget_contract, dict)
            or set(runtime_budget_contract) != {"bytes", "sha256"}
            or runtime_budget_contract.get("sha256")
            != authorization.get("final_runtime_budget_file_sha256")
        ):
            return None
        artifact_snapshots: dict[str, bytes] = {}
        for name, contract in artifacts.items():
            raw = _bound_file_snapshot(
                root, name, contract, top_level=True
            )
            if raw is None:
                return None
            artifact_snapshots[name] = raw
        for name, contract in donor_files.items():
            if _bound_file_snapshot(root, name, contract) is None:
                return None
        evidence_contracts = {
            "artifact_manifest.json": "gate_artifact_manifest_sha256",
            "policy_gate_evaluation.json": "gate_evaluation_sha256",
            "policy_gate_result.json": "gate_result_sha256",
            "policy_gate_report.json": "gate_report_sha256",
        }
        evidence_snapshots: dict[str, bytes] = {}
        for name, field in evidence_contracts.items():
            raw = _file_snapshot(
                root, f"evidence/offline_policy_gate/{name}"
            )
            if (
                raw is None
                or hashlib.sha256(raw).hexdigest()
                != authorization.get(field)
            ):
                return None
            evidence_snapshots[name] = raw
        gate_documents = {
            name: json.loads(raw)
            for name, raw in evidence_snapshots.items()
        }
        if any(not isinstance(document, dict) for document in gate_documents.values()):
            return None
        artifact = gate_documents["artifact_manifest.json"]
        evaluation = gate_documents["policy_gate_evaluation.json"]
        result = gate_documents["policy_gate_result.json"]
        report = gate_documents["policy_gate_report.json"]
        gate_files = artifact.get("files")
        if (
            artifact.get("schema") != GATE_ARTIFACT_SCHEMA
            or not isinstance(gate_files, dict)
            or set(gate_files) != {
                "policy_gate_evaluation.json",
                "policy_gate_result.json",
                "policy_gate_report.json",
            }
        ):
            return None
        for name, contract in gate_files.items():
            raw = evidence_snapshots[name]
            if (
                not isinstance(contract, dict)
                or set(contract) != {"bytes", "sha256"}
                or contract.get("bytes") != len(raw)
                or contract.get("sha256")
                != hashlib.sha256(raw).hexdigest()
            ):
                return None
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
        bundle = json.loads(artifact_snapshots[BUNDLE_FILENAME])
        budget = json.loads(artifact_snapshots[RUNTIME_BUDGET_FILENAME])
        if not isinstance(bundle, dict) or not isinstance(budget, dict):
            return None
        preselection_sha256 = authorization.get(
            "preselection_runtime_budget_payload_sha256"
        )
        identity_sha256 = bundle_runtime_identity_sha256(bundle)
        common_gate_binding = {
            "bundle_bytes": authorization.get("bundle_bytes"),
            "bundle_sha256": authorization.get("bundle_sha256"),
            "preselection_runtime_budget_payload_sha256": preselection_sha256,
            "runtime_identity_sha256": identity_sha256,
            "native_build_contract": authorization.get("native_build_contract"),
        }
        if any(
            document.get(field) != expected
            for document in gate_documents.values()
            for field, expected in common_gate_binding.items()
        ):
            return None
        if (
            evaluation.get("schema") != GATE_EVALUATION_SCHEMA
            or evaluation.get("source_collection_complete") is not True
            or evaluation.get("policy_search_performed") is not False
            or report.get("schema") != GATE_REPORT_SCHEMA
            or report.get("gate_passed") is not True
            or report.get("gate_errors") != []
            or report.get("native_candidate_build_authorized") is not True
            or report.get("source_collection_complete") is not True
            or artifact.get("native_candidate_build_authorized") is not True
            or any(
                document.get("deployment_policy_value") is not False
                or document.get("strength_evidence") is not False
                for document in gate_documents.values()
            )
        ):
            return None
        validated_budget = validate_runtime_budget_artifact(
            budget,
            bundle_bytes=bundle_contract["bytes"],
            bundle_sha256=bundle_contract["sha256"],
            runtime_identity_sha256=identity_sha256,
            preselection_runtime_budget_payload_sha256=preselection_sha256,
            require_formal=True,
        )
        source = bundle.get("source")
        if (
            not isinstance(source, dict)
            or source.get("preselection_runtime_budget_payload_sha256")
            != preselection_sha256
            or source.get("runtime_identity_sha256") != identity_sha256
            or result.get("preselection_runtime_budget_payload_sha256")
            != preselection_sha256
            or result.get("runtime_identity_sha256") != identity_sha256
            or authorization.get("runtime_identity_sha256")
            != identity_sha256
            or validated_budget["payload_sha256"]
            != authorization.get("final_runtime_budget_payload_sha256")
        ):
            return None
        return bundle
    except Exception:
        return None


class NativeV4Policy(NativeV3Policy):
    """Apply the frozen win-first policy with an exact sanitized-rule fallback."""

    def __init__(self, runtime: OpponentMultiTaskEnsembleRuntimeV4) -> None:
        if runtime.policy is None:
            raise ValueError("v4 native policy requires a selected policy")
        self.runtime = runtime
        self.disable_cross_hand = _env_bool("POK_V4_DISABLE_CROSS_HAND")
        self.disable_outcome_uncertainty_match = _env_bool(
            OUTCOME_UNCERTAINTY_MATCH_ABLATION_ENV
        )
        if self.disable_cross_hand and self.disable_outcome_uncertainty_match:
            raise ValueError("v4 diagnostic ablation modes cannot be combined")
        if self.disable_cross_hand:
            self.ablation_mode = ABLATION_MODE_CROSS_HAND_OFF
        elif self.disable_outcome_uncertainty_match:
            self.ablation_mode = ABLATION_MODE_OUTCOME_UNCERTAINTY_MATCH_OFF
        else:
            self.ablation_mode = ABLATION_MODE_FULL
        self.last_decision: dict[str, Any] | None = None

    @staticmethod
    def _outcome_uncertainty_match_ablation_values(
        values: dict[str, dict[str, list[float]]],
    ) -> dict[str, dict[str, list[float]]]:
        projected = {
            field: dict(payload) for field, payload in values.items()
        }
        projected["delta_vs_rule"]["lower"] = list(
            projected["delta_vs_rule"]["mean"]
        )
        for field in ("tail_delta_vs_rule", "match_delta_vs_rule"):
            projected[field]["lower"] = [0.0] * len(LABELS)
        return projected

    @classmethod
    def load(
        cls, source: str | Path | dict[str, Any]
    ) -> "NativeV4Policy | None":
        if _env_bool("POK_V4_DISABLE"):
            return None
        try:
            runtime = (
                OpponentMultiTaskEnsembleRuntimeV4(source)
                if isinstance(source, dict)
                else OpponentMultiTaskEnsembleRuntimeV4.load(source)
            )
            if runtime is None or runtime.policy is None:
                return None
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
        self.last_decision = {
            "used": False,
            "rule_action": safe_rule,
            "ablation_mode": self.ablation_mode,
        }
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
            outcomes = (
                None
                if self.disable_outcome_uncertainty_match
                else self.runtime.predict_match_outcomes(**value_inputs)
            )

            if float(self.runtime.policy["response_weight"]) > 0.0:
                for candidate in alternatives:
                    candidate["response_signal"] = self._response_signal(
                        request, state, legal_mask, candidate
                    )
            else:
                for candidate in alternatives:
                    candidate["response_signal"] = 0.0

            if self.disable_outcome_uncertainty_match:
                values = self._outcome_uncertainty_match_ablation_values(
                    values
                )
                selected = self.runtime.value_response.select_candidate(
                    values, alternatives
                )
            else:
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
                    "ablation_mode": self.ablation_mode,
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
                "ablation_mode": self.ablation_mode,
            }
            return final
        except Exception as exc:
            self.last_decision = {
                "used": False,
                "rule_action": safe_rule,
                "ablation_mode": self.ablation_mode,
                "error": f"{type(exc).__name__}: {exc}",
            }
            return safe_rule


def load_native_v4_policy(bot_dir: str | Path) -> NativeV4Policy | None:
    try:
        bundle = _authorized_bundle_payload(Path(bot_dir))
        return NativeV4Policy.load(bundle) if bundle is not None else None
    except Exception:
        return None
