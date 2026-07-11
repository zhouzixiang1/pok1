"""Lazy, ledger-backed access to opponent-disjoint neural dataset roles."""
from __future__ import annotations

import hashlib
import json
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
POLICY_OFFLINE_ESTIMAND = "single_decision_action_uplift_ipw_v2"
REQUIRED_INVARIANTS = (
    "opponent_disjoint",
    "match_cluster_disjoint",
    "deck_blocks_non_overlapping",
    "uniform_decision_ipw_validated",
    "national_response_v2_validated",
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
        for opponent in self.roles[role]:
            exposures = (
                ledger.get("opponents", {}).get(opponent, {}).get("exposures", [])
            )
            matching = [
                row for row in exposures
                if row.get("role") == role and row.get("run_id") == self.run_id
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
    ) -> str:
        if path is None:
            raise RuntimeError("policy_gate requires a policy-selection result")
        raw = path.resolve().read_bytes()
        try:
            report = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid policy-selection result") from exc
        if (
            not isinstance(report, dict)
            or report.get("schema") != POLICY_SELECTION_RESULT_SCHEMA
            or report.get("passed") is not True
            or report.get("run_id") != self.run_id
            or report.get("candidate_sha256") != candidate_sha256
            or report.get("role_manifest_sha256") != self.manifest_sha256
            or report.get("offline_estimand") != POLICY_OFFLINE_ESTIMAND
            or report.get("deployment_policy_value") is not False
            or report.get("strength_evidence") is not False
            or report.get("policy_gate_opened") is not False
            or report.get("policy_selection_artifact_sha256")
            != self._role_artifact_sha256("policy_selection")
        ):
            raise RuntimeError("policy-selection result did not authorize policy_gate")
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
        return _sha256_bytes(raw)

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
            if filename.startswith("opponent_actions_"):
                try:
                    validate_response_row(row)
                except ValueError as exc:
                    raise RuntimeError(
                        f"invalid response row: {filename}:{line_number}: {exc}"
                    ) from exc
            rows.append(row)
        if len(rows) != expected["rows"]:
            raise RuntimeError(f"role output row count changed: {filename}")
        observed = sorted({_opponent(row) for row in rows})
        if observed != self.roles[role]:
            raise RuntimeError(f"role output opponent coverage changed: {filename}")
        return rows

    def open_role(
        self,
        role: str,
        *,
        candidate_sha256: str | None = None,
        prerequisite_report: Path | None = None,
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
        if role == "policy_gate":
            prerequisite_sha256 = self._policy_gate_report(
                prerequisite_report,
                candidate_sha256=str(candidate_sha256),
            )
            artifact_sha256 = _sha256_bytes(
                f"{artifact_sha256}:{prerequisite_sha256}".encode()
            )
        elif prerequisite_report is not None:
            raise ValueError("prerequisite_report is only valid for policy_gate")

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
            "opponents": list(self.roles[role]),
            "value": value,
            "behavior": behavior,
            "exposure": exposure,
        }
