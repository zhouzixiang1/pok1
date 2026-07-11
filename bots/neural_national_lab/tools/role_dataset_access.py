"""Lazy, ledger-backed access to opponent-disjoint neural dataset roles."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from freeze_opponent_role_dataset import (  # noqa: E402
    EVIDENCE_ROLES,
    PREFIXES,
    SCHEMA as ROLE_DATASET_SCHEMA,
    STRATEGY_CONTEXT_RUNTIME_MODE,
    strategy_context_is_absent,
)
from match_outcome_schema import (  # noqa: E402
    MATCH_OUTCOME_ESTIMAND,
    MATCH_OUTCOME_SCHEMA,
    derive_match_outcome_supervision,
)
from opponent_exposure_ledger import open_exposure, status  # noqa: E402
from opponent_response_schema import (  # noqa: E402
    OPPONENT_RESPONSE_SCHEMA,
    validate_response_row,
)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
POLICY_ROLES = ("policy_selection", "policy_gate")
ROLE_PREREQUISITES = {
    "policy_selection": ("train", "early_stop", "model_calibration"),
    "policy_gate": ("policy_selection",),
}
POLICY_SELECTION_RESULT_SCHEMA = "policy_selection_result_v2"
POLICY_OFFLINE_ESTIMAND = (
    "single_decision_action_uplift_ipw_v3_win_first_70_hand"
)
POLICY_SELECTION_RESULT_SCHEMA_V4 = "policy_selection_result_v3_win_first_v4"
POLICY_OFFLINE_ESTIMAND_V4 = (
    "single_decision_action_uplift_ipw_v4_win_first_70_hand"
)
POLICY_SELECTION_RESULT_CONTRACTS = {
    POLICY_SELECTION_RESULT_SCHEMA: POLICY_OFFLINE_ESTIMAND,
    POLICY_SELECTION_RESULT_SCHEMA_V4: POLICY_OFFLINE_ESTIMAND_V4,
}
REQUIRED_INVARIANTS = (
    "opponent_disjoint",
    "match_cluster_disjoint",
    "deck_blocks_non_overlapping",
    "uniform_decision_ipw_validated",
    "national_response_v2_validated",
    "national_70_hand_outcome_validated",
    "artifact_snapshots_verified",
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be a SHA-256 digest")
    return normalized


def _opponent(row: dict[str, Any]) -> str:
    return str(row.get("_opponent_label") or row.get("opponent") or "").strip()


class RoleDatasetAccess:
    """Read role data only after recording a conservative exposure event."""

    def __init__(
        self,
        manifest_path: Path,
        *,
        ledger_path: Path,
        run_id: str,
        require_complete: bool = True,
    ) -> None:
        self.manifest_path = manifest_path.resolve()
        self.root = self.manifest_path.parent
        self.ledger_path = ledger_path.resolve()
        self.run_id = str(run_id).strip()
        if not self.run_id:
            raise ValueError("run_id is required")
        raw = self.manifest_path.read_bytes()
        try:
            manifest = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid role dataset manifest") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != ROLE_DATASET_SCHEMA:
            raise ValueError("unsupported role dataset manifest")
        self.manifest = manifest
        self.manifest_sha256 = _sha256_bytes(raw)
        if require_complete and manifest.get("source_collection_complete") is not True:
            raise ValueError("role dataset source collection is incomplete")
        invariants = manifest.get("invariants") or {}
        failed = [name for name in REQUIRED_INVARIANTS if invariants.get(name) is not True]
        if failed or invariants.get("final_blind_in_dataset") is not False:
            raise ValueError(f"role dataset invariants failed: {failed}")
        behavior_supervision = manifest.get("behavior_supervision")
        if (
            not isinstance(behavior_supervision, dict)
            or behavior_supervision.get("schema") != OPPONENT_RESPONSE_SCHEMA
        ):
            raise ValueError("role dataset has invalid behavior supervision")
        match_outcome = manifest.get("match_outcome_supervision")
        if (
            not isinstance(match_outcome, dict)
            or match_outcome.get("schema") != MATCH_OUTCOME_SCHEMA
            or match_outcome.get("required_for_win_first_policy_evidence") is not True
        ):
            raise ValueError("role dataset has invalid match outcome supervision")
        candidate = manifest.get("candidate_snapshot")
        candidate_path = (
            str(candidate.get("path") or "").strip()
            if isinstance(candidate, dict) else ""
        )
        candidate_name = (
            str(candidate.get("name") or "").strip()
            if isinstance(candidate, dict) else ""
        )
        if (
            not candidate_path
            or not candidate_name
            or Path(candidate_path).name != candidate_name
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", candidate_name)
        ):
            raise ValueError("role dataset candidate snapshot is invalid")
        self.candidate_snapshot = {
            "name": candidate_name,
            "sha256": _digest(
                candidate.get("sha256"), field="candidate_snapshot.sha256"
            ),
        }
        if (
            manifest.get("strategy_context_runtime_mode")
            != STRATEGY_CONTEXT_RUNTIME_MODE
        ):
            raise ValueError("role dataset strategy-context mode is invalid")
        self.strategy_context_runtime_mode = STRATEGY_CONTEXT_RUNTIME_MODE
        roles = manifest.get("roles")
        outputs = manifest.get("outputs")
        if not isinstance(roles, dict) or set(roles) != set(EVIDENCE_ROLES):
            raise ValueError("role dataset manifest has invalid roles")
        if not isinstance(outputs, dict):
            raise ValueError("role dataset manifest has invalid outputs")
        self.roles = {}
        self.outputs = {}
        for role in EVIDENCE_ROLES:
            names = sorted({str(name).strip() for name in roles[role] if str(name).strip()})
            if not names:
                raise ValueError(f"role has no opponents: {role}")
            self.roles[role] = names
            for prefix in PREFIXES:
                filename = f"{prefix}_{role}.jsonl"
                details = outputs.get(filename)
                if not isinstance(details, dict):
                    raise ValueError(f"role output is missing: {filename}")
                rows = int(details.get("rows", -1))
                size = int(details.get("bytes", -1))
                digest = _digest(details.get("sha256"), field=f"{filename}.sha256")
                opponents = sorted({
                    str(name).strip()
                    for name in details.get("opponents", [])
                    if str(name).strip()
                })
                if rows < 1 or size < 1 or opponents != names:
                    raise ValueError(f"invalid role output contract: {filename}")
                if (
                    prefix == "opponent_actions"
                    and details.get("row_schema") != OPPONENT_RESPONSE_SCHEMA
                ):
                    raise ValueError(f"invalid behavior row schema: {filename}")
                self.outputs[filename] = {
                    "rows": rows,
                    "bytes": size,
                    "sha256": digest,
                    "opponents": opponents,
                }

    def require_collection_boundary(
        self, expected_passes: int = 160
    ) -> dict[str, Any]:
        """Require one complete atomic collection boundary before formal use."""
        if (
            isinstance(expected_passes, bool)
            or not isinstance(expected_passes, int)
            or expected_passes < 1
        ):
            raise ValueError("expected collection passes must be a positive integer")
        completed = self.manifest.get("source_completed_passes")
        requested = self.manifest.get("source_requested_passes")
        if (
            isinstance(completed, bool)
            or not isinstance(completed, int)
            or isinstance(requested, bool)
            or not isinstance(requested, int)
            or completed != expected_passes
            or requested != expected_passes
            or self.manifest.get("source_collection_complete") is not True
        ):
            raise ValueError(
                "formal role dataset requires the complete atomic "
                f"{expected_passes}-pass boundary"
            )
        return {
            "schema": "complete_atomic_collection_boundary_v1",
            "source_completed_passes": completed,
            "source_requested_passes": requested,
            "source_collection_complete": True,
        }

    def _role_artifact_sha256(self, role: str) -> str:
        contract = {
            filename: self.outputs[filename]["sha256"]
            for filename in (
                f"cf_{role}.jsonl", f"opponent_actions_{role}.jsonl"
            )
        }
        return _sha256_bytes(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        )

    def _role_was_opened(
        self,
        role: str,
        *,
        candidate_sha256: str | None,
    ) -> bool:
        ledger = status(self.ledger_path)
        expected_artifact = self._role_artifact_sha256(role)
        for opponent in self.roles[role]:
            exposures = (
                ledger.get("opponents", {}).get(opponent, {}).get("exposures", [])
            )
            matching = [
                row for row in exposures
                if row.get("role") == role and row.get("run_id") == self.run_id
                and row.get("artifact_sha256") == expected_artifact
            ]
            if candidate_sha256 is not None:
                matching = [
                    row for row in matching
                    if row.get("candidate_sha256") == candidate_sha256
                ]
            if not matching:
                return False
        return True

    def _check_prerequisites(
        self, role: str, *, candidate_sha256: str | None
    ) -> None:
        for prerequisite in ROLE_PREREQUISITES.get(role, ()):
            required_candidate = (
                candidate_sha256 if prerequisite in POLICY_ROLES else None
            )
            if not self._role_was_opened(
                prerequisite, candidate_sha256=required_candidate
            ):
                raise RuntimeError(
                    f"role prerequisite was not opened by this run: "
                    f"{prerequisite} -> {role}"
                )

    def _policy_gate_report(
        self,
        path: Path | None,
        *,
        candidate_sha256: str,
        expected_schema: str | None = None,
        expected_offline_estimand: str | None = None,
    ) -> dict[str, str]:
        if path is None:
            raise RuntimeError("policy_gate requires a policy-selection result")
        raw = path.resolve().read_bytes()
        try:
            report = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid policy-selection result") from exc
        if (
            not isinstance(report, dict)
            or report.get("schema") not in POLICY_SELECTION_RESULT_CONTRACTS
            or POLICY_SELECTION_RESULT_CONTRACTS[report.get("schema")]
            != report.get("offline_estimand")
            or (
                expected_schema is not None
                and report.get("schema") != expected_schema
            )
            or (
                expected_offline_estimand is not None
                and report.get("offline_estimand")
                != expected_offline_estimand
            )
            or report.get("passed") is not True
            or report.get("run_id") != self.run_id
            or report.get("candidate_sha256") != candidate_sha256
            or report.get("role_manifest_sha256") != self.manifest_sha256
            or report.get("match_outcome_estimand") != MATCH_OUTCOME_ESTIMAND
            or report.get("deployment_policy_value") is not False
            or report.get("strength_evidence") is not False
            or report.get("policy_gate_opened") is not False
            or report.get("policy_selection_artifact_sha256")
            != self._role_artifact_sha256("policy_selection")
        ):
            raise RuntimeError("policy-selection result did not authorize policy_gate")
        if report.get("schema") == POLICY_SELECTION_RESULT_SCHEMA_V4:
            thresholds = report.get("thresholds")
            summary = report.get("summary")
            bootstrap = (
                summary.get("bootstrap_contract")
                if isinstance(summary, dict) else None
            )
            threshold_fields = {
                "min_overrides",
                "min_selection_clusters",
                "min_override_clusters",
                "min_overrides_per_opponent",
                "min_override_hand_mean",
                "bootstrap_samples",
                "min_cluster_ci_lower",
                "min_opponent_stratified_ci_lower",
                "min_match_outcome_coverage",
                "min_match_positive_rate_ci_lower",
                "min_match_positive_uplift_ci_lower",
                "min_opponent_match_positive_rate",
            }
            integer_floors = {
                "min_overrides": 12,
                "min_selection_clusters": 8,
                "min_override_clusters": 8,
                "min_overrides_per_opponent": 4,
                "bootstrap_samples": 2000,
            }
            valid_integers = isinstance(thresholds, dict) and all(
                not isinstance(thresholds.get(field), bool)
                and isinstance(thresholds.get(field), int)
                and thresholds[field] >= floor
                for field, floor in integer_floors.items()
            )
            numeric_fields = threshold_fields - set(integer_floors)
            valid_numbers = isinstance(thresholds, dict) and all(
                not isinstance(thresholds.get(field), bool)
                and isinstance(thresholds.get(field), (int, float))
                and math.isfinite(float(thresholds[field]))
                for field in numeric_fields
            )
            if (
                report.get("errors") != []
                or report.get("formal_selection") is not True
                or report.get("source_collection_complete") is not True
                or report.get("candidate_snapshot") != self.candidate_snapshot
                or report.get("strategy_context_runtime_mode")
                != self.strategy_context_runtime_mode
                or not isinstance(thresholds, dict)
                or set(thresholds) != threshold_fields
                or not valid_integers
                or not valid_numbers
                or thresholds.get("min_override_hand_mean", -1.0) < 0.0
                or thresholds.get("min_cluster_ci_lower", -1.0) < 0.0
                or thresholds.get(
                    "min_opponent_stratified_ci_lower", -1.0
                ) < 0.0
                or thresholds.get("min_match_outcome_coverage") != 1.0
                or thresholds.get("min_match_positive_rate_ci_lower", 0.0)
                < 0.5
                or thresholds.get(
                    "min_match_positive_uplift_ci_lower", -1.0
                ) < 0.0
                or thresholds.get("min_opponent_match_positive_rate", 0.0)
                < 0.5
                or not isinstance(bootstrap, dict)
                or set(bootstrap) != {
                    "schema",
                    "samples",
                    "seed",
                    "observed_70_hand_match_clusters",
                    "ordinary",
                    "opponent_stratified",
                }
                or bootstrap.get("schema")
                != "observed_70_hand_match_cluster_bootstrap_v1"
                or bootstrap.get("samples")
                != thresholds.get("bootstrap_samples")
                or isinstance(bootstrap.get("seed"), bool)
                or not isinstance(bootstrap.get("seed"), int)
                or bootstrap.get("observed_70_hand_match_clusters") is not True
                or bootstrap.get("ordinary") is not True
                or bootstrap.get("opponent_stratified") is not True
            ):
                raise RuntimeError(
                    "policy-selection result did not authorize policy_gate"
                )
        for field in (
            "calibration_payload_sha256",
            "evaluation_report_sha256",
            "selected_policy_sha256",
        ):
            try:
                _digest(report.get(field), field=field)
            except ValueError as exc:
                raise RuntimeError(
                    "policy-selection result did not authorize policy_gate"
                ) from exc
        return {
            "sha256": _sha256_bytes(raw),
            "calibration_payload_sha256": _digest(
                report.get("calibration_payload_sha256"),
                field="calibration_payload_sha256",
            ),
        }

    def _read_output(self, filename: str, role: str) -> list[dict[str, Any]]:
        path = self.root / filename
        raw = path.read_bytes()
        expected = self.outputs[filename]
        if len(raw) != expected["bytes"] or _sha256_bytes(raw) != expected["sha256"]:
            raise RuntimeError(f"role output artifact changed: {filename}")
        rows = []
        for line_number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"invalid role row: {filename}:{line_number}") from exc
            if not isinstance(row, dict):
                raise RuntimeError(f"role row must be an object: {filename}:{line_number}")
            if row.get("_evidence_role") != role or row.get("_split") != role:
                raise RuntimeError(f"role row label mismatch: {filename}:{line_number}")
            if _opponent(row) not in self.roles[role]:
                raise RuntimeError(f"role row opponent mismatch: {filename}:{line_number}")
            if not strategy_context_is_absent(row):
                raise RuntimeError(
                    "zero-context role dataset contains strategy context: "
                    f"{filename}:{line_number}"
                )
            if filename.startswith("opponent_actions_"):
                try:
                    validate_response_row(row)
                except ValueError as exc:
                    raise RuntimeError(
                        f"invalid response row: {filename}:{line_number}: {exc}"
                    ) from exc
            else:
                try:
                    derive_match_outcome_supervision(row, required=True)
                except ValueError as exc:
                    raise RuntimeError(
                        f"invalid match outcome row: "
                        f"{filename}:{line_number}: {exc}"
                    ) from exc
            rows.append(row)
        if len(rows) != expected["rows"]:
            raise RuntimeError(f"role output row count changed: {filename}")
        observed = sorted({_opponent(row) for row in rows})
        if observed != self.roles[role]:
            raise RuntimeError(f"role output opponent coverage changed: {filename}")
        return rows

    def runtime_context_contract(self) -> dict[str, Any]:
        return {
            "candidate_snapshot": dict(self.candidate_snapshot),
            "strategy_context_runtime_mode": self.strategy_context_runtime_mode,
        }

    def open_role(
        self,
        role: str,
        *,
        candidate_sha256: str | None = None,
        prerequisite_report: Path | None = None,
        prerequisite_schema: str | None = None,
        prerequisite_offline_estimand: str | None = None,
    ) -> dict[str, Any]:
        if role not in EVIDENCE_ROLES:
            raise ValueError(f"unsupported evidence role: {role}")
        if role in POLICY_ROLES:
            candidate_sha256 = _digest(
                candidate_sha256, field="candidate_sha256"
            )
        elif candidate_sha256 is not None:
            candidate_sha256 = _digest(
                candidate_sha256, field="candidate_sha256"
            )
        self._check_prerequisites(role, candidate_sha256=candidate_sha256)
        artifact_sha256 = self._role_artifact_sha256(role)
        prerequisite_sha256 = None
        prerequisite_calibration_payload_sha256 = None
        if role == "policy_gate":
            prerequisite = self._policy_gate_report(
                prerequisite_report,
                candidate_sha256=str(candidate_sha256),
                expected_schema=prerequisite_schema,
                expected_offline_estimand=prerequisite_offline_estimand,
            )
            prerequisite_sha256 = prerequisite["sha256"]
            prerequisite_calibration_payload_sha256 = prerequisite[
                "calibration_payload_sha256"
            ]
            artifact_sha256 = _sha256_bytes(
                f"{artifact_sha256}:{prerequisite_sha256}".encode()
            )
        elif any(value is not None for value in (
            prerequisite_report,
            prerequisite_schema,
            prerequisite_offline_estimand,
        )):
            raise ValueError("prerequisite contract is only valid for policy_gate")

        # Exposure is recorded before touching either role file. If validation
        # then fails, the opponent remains conservatively exposed.
        exposure = open_exposure(
            self.ledger_path,
            role=role,
            opponents=self.roles[role],
            run_id=self.run_id,
            candidate_sha256=candidate_sha256,
            artifact_sha256=artifact_sha256,
        )
        value = self._read_output(f"cf_{role}.jsonl", role)
        behavior = self._read_output(f"opponent_actions_{role}.jsonl", role)
        return {
            "role": role,
            "candidate_sha256": candidate_sha256,
            "artifact_sha256": artifact_sha256,
            "manifest_sha256": self.manifest_sha256,
            "prerequisite_sha256": prerequisite_sha256,
            "prerequisite_calibration_payload_sha256": (
                prerequisite_calibration_payload_sha256
            ),
            "opponents": list(self.roles[role]),
            "value": value,
            "behavior": behavior,
            "exposure": exposure,
        }
