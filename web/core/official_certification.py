"""Official EXE certification and signed evidence validation.

The fast national-native gates own strength tracking and regression. This module
tracks slower official Windows platform evidence separately as a compliance
oracle: real protocol violations can block future parent selection, but EXE
runtime/infrastructure ambiguity must not become the evolution score or
multi-generation tracking loop.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Literal

from bot_artifact import canonical_digest, hash_path, published_bot_identity
from bot_namespace import FIRST_STRICT_POLICY_VERSION, bot_name, parse_bot_version
from official_eligibility import epoch_lifecycle_eligibility, strict_role_eligibility
from official_platform_harness import (
    OfficialPlatformConfig,
    _copy_config,
    round_completion_issues,
    run_official_acceptance_sync,
)
from official_evidence import build_official_evidence_bundle
from official_evidence_archive import (
    build_evidence_archive,
    validate_evidence_archive,
    validate_evidence_archive_receipt,
)
from official_attribution import round_topology
from official_platform_resource import official_platform_busy


ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "web" / "core" / "results"
DEFAULT_CERT_DIR = RESULTS_DIR / "official_certification"
PUBLISHED_CERTIFICATE_DIR = ROOT / "official_certificates"
HARNESS_PATH = ROOT / "web" / "core" / "official_platform_harness.py"
SERVICE_PATH = Path(__file__).resolve()
ATTRIBUTION_PATH = ROOT / "web" / "core" / "official_attribution.py"
EVIDENCE_PATH = ROOT / "web" / "core" / "official_evidence.py"
WIRE_PROBE_PATH = ROOT / "web" / "core" / "official_wire_probe.py"
LLM_ANALYSIS_PATH = ROOT / "web" / "core" / "official_llm_analysis.py"
LLM_ANALYSIS_PROMPT_PATH = ROOT / "web" / "core" / "prompts" / "official_platform_analysis.md"
PLATFORM_RESOURCE_PATH = ROOT / "web" / "core" / "official_platform_resource.py"
EVIDENCE_ARCHIVE_PATH = ROOT / "web" / "core" / "official_evidence_archive.py"
CERTIFICATE_SIGNING_PATH = ROOT / "web" / "core" / "official_certificate_signing.py"
CERTIFIER_TRUST_ROOT_PATH = ROOT / "web" / "core" / "official_certifier_allowed_signers"
CERTIFIER_TRUST_POLICY_PATH = ROOT / "web" / "core" / "official_certifier_trust_policy.json"
EXECUTION_PROFILE_PATH = ROOT / "web" / "core" / "official_execution_profile.json"
EXECUTION_PROFILE_CODE_PATH = ROOT / "web" / "core" / "official_execution_profile.py"
BOT_SANDBOX_PATH = ROOT / "web" / "core" / "official_bot_sandbox.py"
MANAGED_EXECUTOR_PATH = ROOT / "web" / "core" / "managed_bot_executor.py"
MANAGED_SOCKET_PATH = ROOT / "web" / "core" / "managed_bot_socket.py"

CertificationMode = Literal["smoke", "compliance", "full"]
FULL_POLICY_ID = "official-full-v5"
CERTIFICATE_SCHEMA_VERSION = 5
PUBLISHED_ATTESTATION_SCHEMA_VERSION = 2
DETERMINISTIC_RECEIPT_SCHEMA_VERSION = 3
DETERMINISTIC_STATUS_RECEIPT_SCHEMA_VERSION = 1
OPPONENT_ELIGIBILITY_RECEIPT_SCHEMA_VERSION = 1
RETIRED_BOOTSTRAP_SPEC_FIELDS = frozenset({
    "bootstrap_root_id",
    "bootstrap_root_receipt",
})

STATUS_LOCAL_PASS = "local-pass"
STATUS_SMOKE_PASS = "official-smoke-pass"
STATUS_COMPLIANCE_PASS = "official-compliance-pass"
STATUS_PENDING = "official-pending"
STATUS_CERTIFIED = "official-certified"
STATUS_INCONCLUSIVE = "official-inconclusive"
STATUS_FAILED = "official-failed"
STATUS_UNCERTIFIED = "official-uncertified"

PARENT_BLOCKING_FAILURE_MARKERS = (
    "protocol_",
    "protocol error",
    "illegal",
    "invalid action",
    "unknown action",
    "illegal_bet_action",
    "protocol_raise_format",
    "protocol_action_format",
    "protocol_action_whitespace",
    "platform_silent_timeout_gap",
    "official_log_silent_timeout_gap",
    "official_full_round_incomplete_after_progress",
    "official_full_early_platform_close_after_progress",
)

OFFICIAL_DECISION_FAILURE_MARKERS = (
    "official_full_round_incomplete_after_progress",
    "official_full_early_platform_close_after_progress",
)

COMPLIANCE_INCONCLUSIVE_FAILURE_MARKERS = (
    "port_busy_before_start",
    "official_platform_lock_timeout",
    "missing_tools:",
    "exe_missing:",
    "wineprefix_missing:",
    "official_acceptance_suite_exception",
    "official_round_exception",
    "official platform did not listen",
    "official platform window not found",
    "platform_exited_early",
    "no_progress_timeout",
    "round_timeout",
    "incomplete_round",
    "official_full_settlement_incomplete",
    "thp_missing_for_full_70_hand_round",
    "thp_incomplete",
    "smoke_progress_incomplete",
    "round_count_mismatch",
    "receipt_not_passed",
    "report_not_passed",
    "file not found",
    "filenotfounderror",
    "wine",
    "xvfb",
    "xdotool",
)

MODE_CONFIG = {
    "smoke": {
        "self_play_rounds": 1,
        "opponent_rounds": 1,
        "target_hands": 10,
        "round_timeout_sec": 180.0,
        "no_progress_timeout_sec": 60.0,
    },
    "compliance": {
        "self_play_rounds": 1,
        "opponent_rounds": 1,
        "target_hands": 10,
        "round_timeout_sec": 180.0,
        "no_progress_timeout_sec": 60.0,
    },
    "full": {
        "self_play_rounds": 5,
        "opponent_rounds": 3,
        "target_hands": 70,
        "round_timeout_sec": 900.0,
        "no_progress_timeout_sec": 75.0,
    },
}


@dataclass(frozen=True)
class CertificationSpec:
    mode: CertificationMode
    policy_id: str
    candidate: str
    opponent: str | None
    self_play_rounds: int
    opponent_rounds: int
    target_hands: int
    round_timeout_sec: float
    no_progress_timeout_sec: float
    # The normal formal path deliberately leaves this unset.  The only legal
    # value identifies the current system-owned first-strict control.
    bootstrap_control_id: str | None = None


def spec_record(spec: CertificationSpec) -> dict[str, Any]:
    """Serialize a spec without changing legacy v5 identity bytes.

    Omitting the ``None`` default keeps ordinary full-v5 identities compact;
    an explicit first-strict control authorization is identity-bearing.
    """
    record = asdict(spec)
    if record.get("bootstrap_control_id") is None:
        record.pop("bootstrap_control_id", None)
    return record


Runner = Callable[..., Any]
PRODUCTION_RUNNER_PROVENANCE = "official-exe"
TEST_ONLY_RUNNER_PROVENANCE = "test-only-injected-runner"
_PRODUCTION_CERTIFICATION_RUNNER = run_official_acceptance_sync
_PRODUCTION_FULL_AUTHORITY = object()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def certification_root() -> Path:
    return Path(os.environ.get("POK_OFFICIAL_CERT_DIR", str(DEFAULT_CERT_DIR)))


def status_dir() -> Path:
    return certification_root() / "status"


def cache_dir() -> Path:
    return certification_root() / "cache"


def certificate_dir() -> Path:
    return certification_root() / "certificates"


def published_certificate_path(candidate: str | Path) -> Path:
    return PUBLISHED_CERTIFICATE_DIR / f"{_safe_label(candidate)}.json"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dataclass_fields__"):
        return {key: _jsonable(val) for key, val in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _attestation_payload_digest(payload: dict[str, Any]) -> str:
    return canonical_digest({
        key: value
        for key, value in payload.items()
        if key != "attestation_digest"
    })


def _load_certificate_container(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return None, None, ["content_bound_certificate_missing"]
    if "certificate" not in payload:
        return payload, None, []

    issues: list[str] = []
    record = payload.get("certificate")
    if payload.get("schema_version") != PUBLISHED_ATTESTATION_SCHEMA_VERSION:
        issues.append("published_attestation_schema_mismatch")
    if payload.get("kind") != "official-platform-compliance-attestation":
        issues.append("published_attestation_kind_mismatch")
    expected_digest = str(payload.get("attestation_digest") or "")
    if not expected_digest or expected_digest != _attestation_payload_digest(payload):
        issues.append("published_attestation_digest_mismatch")
    if not isinstance(record, dict):
        return None, payload, [*issues, "published_attestation_certificate_missing"]
    if payload.get("certificate_digest") != record.get("certificate_digest"):
        issues.append("published_attestation_certificate_digest_mismatch")
    signature = str(payload.get("signature") or "")
    if hashlib.sha256(signature.encode("utf-8")).hexdigest() != payload.get("signature_sha256"):
        issues.append("published_attestation_signature_digest_mismatch")
    if payload.get("issuer") != record.get("issuer"):
        issues.append("published_attestation_issuer_mismatch")
    return record, payload, issues


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _safe_label(path_or_token: str | Path) -> str:
    path = Path(path_or_token)
    name = path.name or str(path_or_token)
    version = parse_bot_version(name)
    return bot_name(version) if version else name.replace("/", "_")


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _certificate_file_manifest(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    if not path.is_file():
        raise ValueError(f"{label} is missing or not a regular file: {path}")
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": _file_sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _validate_certificate_file_manifest(
    manifest: Any,
    *,
    label: str,
) -> tuple[Path | None, list[str]]:
    if not isinstance(manifest, dict):
        return None, [f"certificate_{label}_manifest_missing"]
    path_value = str(manifest.get("path") or "").strip()
    expected_hash = str(manifest.get("sha256") or "").strip()
    if not path_value or not expected_hash or expected_hash == "missing":
        return None, [f"certificate_{label}_manifest_incomplete"]
    path = Path(path_value)
    if path.is_symlink():
        return None, [f"certificate_{label}_symlink"]
    if not path.is_file():
        return None, [f"certificate_{label}_missing"]
    issues: list[str] = []
    try:
        if _file_sha256(path) != expected_hash:
            issues.append(f"certificate_{label}_sha256_mismatch")
        expected_size = manifest.get("size_bytes")
        if expected_size is not None and path.stat().st_size != int(expected_size):
            issues.append(f"certificate_{label}_size_mismatch")
    except Exception as exc:
        issues.append(
            f"certificate_{label}_read_error:{type(exc).__name__}:{str(exc)[:160]}"
        )
    return path, issues


def _iter_evidence_artifact_manifests(evidence: dict[str, Any]):
    for round_index, round_item in enumerate(evidence.get("rounds") or [], start=1):
        if not isinstance(round_item, dict):
            continue
        artifacts = round_item.get("artifacts") or {}
        if not isinstance(artifacts, dict):
            continue
        for key, value in artifacts.items():
            items = value if isinstance(value, list) else [value]
            for item_index, item in enumerate(items, start=1):
                if isinstance(item, dict) and item.get("path"):
                    yield f"round_{round_index}_{key}_{item_index}", item


def _validate_retained_evidence_artifacts(evidence_path: Path) -> list[str]:
    evidence = _read_json(evidence_path)
    if not isinstance(evidence, dict):
        return ["certificate_evidence_json_invalid"]
    issues: list[str] = []
    manifests = list(_iter_evidence_artifact_manifests(evidence))
    if not manifests:
        return ["certificate_evidence_artifact_manifest_missing"]
    for label, manifest in manifests:
        if manifest.get("exists") is not True:
            issues.append(f"certificate_retained_artifact_missing:{label}")
            continue
        _path, item_issues = _validate_certificate_file_manifest(
            manifest,
            label=f"retained_artifact_{label}",
        )
        issues.extend(item_issues)
    return issues


def _mode_defaults(mode: CertificationMode) -> dict[str, Any]:
    return dict(MODE_CONFIG[mode])


def build_spec(
    mode: CertificationMode,
    candidate: str | Path,
    *,
    opponent: str | Path | None = None,
    self_play_rounds: int | None = None,
    opponent_rounds: int | None = None,
    target_hands: int | None = None,
    round_timeout_sec: float | None = None,
    no_progress_timeout_sec: float | None = None,
    bootstrap_control_id: str | None = None,
) -> CertificationSpec:
    defaults = _mode_defaults(mode)
    requested = {
        "self_play_rounds": int(
            self_play_rounds if self_play_rounds is not None else defaults["self_play_rounds"]
        ),
        "opponent_rounds": int(
            opponent_rounds if opponent_rounds is not None else defaults["opponent_rounds"]
        ),
        "target_hands": max(
            1,
            min(70, int(target_hands if target_hands is not None else defaults["target_hands"])),
        ),
    }
    if mode == "full":
        mismatches = {
            key: {"required": int(defaults[key]), "requested": value}
            for key, value in requested.items()
            if value != int(defaults[key])
        }
        if mismatches:
            raise ValueError(
                "full official certification profile is immutable: "
                + json.dumps(mismatches, sort_keys=True)
            )
        if opponent is None:
            raise ValueError("full official certification requires an opponent")
    spec = CertificationSpec(
        mode=mode,
        policy_id=FULL_POLICY_ID if mode == "full" else f"official-{mode}-v1",
        candidate=str(Path(candidate).expanduser().resolve()),
        opponent=str(Path(opponent).expanduser().resolve()) if opponent else None,
        self_play_rounds=requested["self_play_rounds"],
        opponent_rounds=requested["opponent_rounds"],
        target_hands=requested["target_hands"],
        round_timeout_sec=float(round_timeout_sec if round_timeout_sec is not None else defaults["round_timeout_sec"]),
        no_progress_timeout_sec=float(
            no_progress_timeout_sec if no_progress_timeout_sec is not None else defaults["no_progress_timeout_sec"]
        ),
        bootstrap_control_id=(
            str(bootstrap_control_id).strip()
            if bootstrap_control_id is not None
            else None
        ),
    )
    validate_spec(spec)
    return spec


def validate_spec(spec: CertificationSpec) -> None:
    if spec.mode not in MODE_CONFIG:
        raise ValueError(f"unknown official certification mode: {spec.mode!r}")
    if spec.self_play_rounds < 0 or spec.opponent_rounds < 0:
        raise ValueError("official certification rounds cannot be negative")
    if spec.self_play_rounds + spec.opponent_rounds <= 0:
        raise ValueError("official certification must run at least one round")
    if spec.bootstrap_control_id is not None:
        if spec.mode != "full":
            raise ValueError("bootstrap control is valid only for immutable full certification")
        from first_strict_control import CONTROL_ID

        if spec.bootstrap_control_id != CONTROL_ID:
            raise ValueError("unknown first-strict bootstrap control id")
        if parse_bot_version(Path(spec.candidate).name) != FIRST_STRICT_POLICY_VERSION:
            raise ValueError(
                "first-strict bootstrap control is valid only for national_v143"
            )
    if spec.mode == "full":
        defaults = MODE_CONFIG["full"]
        valid = (
            spec.policy_id == FULL_POLICY_ID
            and spec.self_play_rounds == defaults["self_play_rounds"]
            and spec.opponent_rounds == defaults["opponent_rounds"]
            and spec.target_hands == defaults["target_hands"]
            and bool(spec.opponent)
        )
        if not valid:
            raise ValueError(
                f"full certification must use {FULL_POLICY_ID} with "
                "5 self + 3 opponent rounds of 70 hands"
            )


def _config_fingerprint(config: OfficialPlatformConfig) -> dict[str, Any]:
    from managed_bot_executor import managed_executor_identity

    return {
        "exe_path": str(config.exe_path),
        "exe_sha256": _file_sha256(config.exe_path) if config.exe_path.exists() else "missing",
        "harness_sha256": _file_sha256(HARNESS_PATH) if HARNESS_PATH.exists() else "missing",
        "service_sha256": _file_sha256(SERVICE_PATH) if SERVICE_PATH.exists() else "missing",
        "attribution_sha256": _file_sha256(ATTRIBUTION_PATH) if ATTRIBUTION_PATH.exists() else "missing",
        "evidence_sha256": _file_sha256(EVIDENCE_PATH) if EVIDENCE_PATH.exists() else "missing",
        "wire_probe_sha256": _file_sha256(WIRE_PROBE_PATH) if WIRE_PROBE_PATH.exists() else "missing",
        "platform_resource_sha256": (
            _file_sha256(PLATFORM_RESOURCE_PATH)
            if PLATFORM_RESOURCE_PATH.exists()
            else "missing"
        ),
        "evidence_archive_sha256": (
            _file_sha256(EVIDENCE_ARCHIVE_PATH)
            if EVIDENCE_ARCHIVE_PATH.exists()
            else "missing"
        ),
        "certificate_signing_sha256": (
            _file_sha256(CERTIFICATE_SIGNING_PATH)
            if CERTIFICATE_SIGNING_PATH.exists()
            else "missing"
        ),
        "execution_profile_sha256": (
            _file_sha256(EXECUTION_PROFILE_PATH)
            if EXECUTION_PROFILE_PATH.exists()
            else "missing"
        ),
        "execution_profile_code_sha256": (
            _file_sha256(EXECUTION_PROFILE_CODE_PATH)
            if EXECUTION_PROFILE_CODE_PATH.exists()
            else "missing"
        ),
        "bot_sandbox_sha256": (
            _file_sha256(BOT_SANDBOX_PATH)
            if BOT_SANDBOX_PATH.exists()
            else "missing"
        ),
        "certifier_trust_root_sha256": (
            _file_sha256(CERTIFIER_TRUST_ROOT_PATH)
            if CERTIFIER_TRUST_ROOT_PATH.exists()
            else "missing"
        ),
        "certifier_trust_policy_sha256": (
            _file_sha256(CERTIFIER_TRUST_POLICY_PATH)
            if CERTIFIER_TRUST_POLICY_PATH.exists()
            else "missing"
        ),
        "managed_executor_sha256": (
            _file_sha256(MANAGED_EXECUTOR_PATH)
            if MANAGED_EXECUTOR_PATH.exists()
            else "missing"
        ),
        "managed_socket_sha256": (
            _file_sha256(MANAGED_SOCKET_PATH)
            if MANAGED_SOCKET_PATH.exists()
            else "missing"
        ),
        "managed_executor_identity": managed_executor_identity(),
        "host": config.host,
        "port": config.port,
        "wineprefix": str(config.wineprefix),
        "startup_timeout_sec": config.startup_timeout_sec,
        "listen_timeout_sec": config.listen_timeout_sec,
        "no_progress_timeout_sec": config.no_progress_timeout_sec,
        "round_timeout_sec": config.round_timeout_sec,
        "settlement_grace_sec": config.settlement_grace_sec,
        "artifact_grace_sec": config.artifact_grace_sec,
        "ui": asdict(config.ui),
    }


def certification_identity(
    spec: CertificationSpec,
    config: OfficialPlatformConfig | None = None,
    *,
    runner_provenance: str = PRODUCTION_RUNNER_PROVENANCE,
    test_only: bool = False,
) -> dict[str, Any]:
    validate_spec(spec)
    cfg = _copy_config(
        config or OfficialPlatformConfig(),
        round_timeout_sec=spec.round_timeout_sec,
        no_progress_timeout_sec=spec.no_progress_timeout_sec,
        results_dir=certification_root() / spec.mode,
    )
    payload = {
        "schema_version": CERTIFICATE_SCHEMA_VERSION,
        "policy_id": spec.policy_id,
        "runner_provenance": runner_provenance,
        "authority_scope": "test-only" if test_only else "production",
        "test_only": bool(test_only),
        "spec": spec_record(spec),
        "candidate_hash": hash_path(spec.candidate),
        "opponent_hash": hash_path(spec.opponent) if spec.opponent else None,
        "platform": _config_fingerprint(cfg),
    }
    payload["platform_fingerprint"] = canonical_digest(payload["platform"])
    payload["identity_digest"] = canonical_digest(payload)
    return payload


def cache_key(spec: CertificationSpec, config: OfficialPlatformConfig | None = None) -> str:
    return str(certification_identity(spec, config)["identity_digest"])


def _cache_file(key: str) -> Path:
    return cache_dir() / f"{key}.json"


def _max_thp_hands(receipt: Any) -> int:
    if not isinstance(receipt, dict):
        return 0
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        return 0
    summaries = artifacts.get("thp_summaries") or []
    if not isinstance(summaries, list):
        return 0
    values = []
    for item in summaries:
        if not isinstance(item, dict):
            values.append(0)
            continue
        try:
            values.append(int(item.get("hand_records", 0) or 0))
        except Exception:
            values.append(0)
    return max(values, default=0)


def _formal_thp_artifact_issues(
    receipt: dict[str, Any],
    *,
    expected_hands: int,
) -> list[str]:
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    canonical = artifacts.get("canonical_thp")
    if not isinstance(canonical, dict):
        return ["canonical_thp_missing_for_full_certification"]
    issues: list[str] = []
    path_value = canonical.get("path")
    try:
        path = Path(str(path_value)) if path_value else None
        regular = bool(path) and path.is_file() and not path.is_symlink()
    except Exception:
        path = None
        regular = False
    if not regular or path is None:
        return ["canonical_thp_artifact_missing"]
    listed = artifacts.get("thp_files") or []
    if not isinstance(listed, list):
        listed = [listed]
    try:
        listed_paths = {
            Path(str(value)).expanduser().resolve()
            for value in listed
            if value
        }
        if path.expanduser().resolve() not in listed_paths:
            issues.append("canonical_thp_not_in_artifact_list")
    except Exception:
        issues.append("canonical_thp_artifact_list_invalid")
    try:
        raw = path.read_bytes()
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        text = raw.decode("gb2312", errors="replace")
        actual_indices = [
            int(value)
            for value in re.findall(r"\bSTATE:(\d+):", text)
        ]
        actual_hands = len(actual_indices)
    except Exception as exc:
        return [f"canonical_thp_read_error:{type(exc).__name__}"]
    if canonical.get("sha256") != actual_sha256:
        issues.append("canonical_thp_sha256_mismatch")
    try:
        claimed_hands = int(canonical.get("hand_records", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        claimed_hands = 0
    if claimed_hands != actual_hands:
        issues.append(
            "canonical_thp_summary_count_mismatch: "
            f"claimed={claimed_hands} actual={actual_hands}"
        )
    if actual_hands != expected_hands:
        issues.append(
            "thp_hand_count_mismatch_for_full_certification: "
            f"hands={actual_hands} expected={expected_hands}"
        )
    if actual_indices != list(range(expected_hands)):
        issues.append(
            "thp_hand_index_sequence_mismatch_for_full_certification: "
            f"expected=0..{max(0, expected_hands - 1)}"
        )
    summaries = artifacts.get("thp_summaries") or []
    if not isinstance(summaries, list) or not summaries:
        issues.append("thp_summaries_missing_for_full_certification")
    else:
        digests = {
            str(item.get("sha256") or "")
            for item in summaries
            if isinstance(item, dict) and item.get("exists") is True and not item.get("issue")
        }
        if digests != {actual_sha256}:
            issues.append("thp_outputs_not_single_content_identity")
    return issues


def _formal_execution_issues(receipt: dict[str, Any]) -> list[str]:
    from managed_bot_executor import IsolationIdentity
    from official_execution_profile import (
        execution_profile_identity,
        load_execution_profile,
    )

    expected_profile = execution_profile_identity()
    execution = (
        receipt.get("formal_execution")
        if isinstance(receipt.get("formal_execution"), dict)
        else {}
    )
    issues: list[str] = []
    if execution.get("sandboxed") is not True:
        issues.append("official_formal_bot_sandbox_missing")
    for key, value in expected_profile.items():
        if execution.get(key) != value:
            issues.append(f"official_formal_execution_{key}_mismatch")
    bot_a = receipt.get("bot_a") if isinstance(receipt.get("bot_a"), dict) else {}
    bot_b = receipt.get("bot_b") if isinstance(receipt.get("bot_b"), dict) else {}
    expected_hashes: dict[str, str] = {}
    for label, launch in (("a", bot_a), ("b", bot_b)):
        launch_path = launch.get("path")
        if launch_path:
            try:
                expected_hash = hash_path(str(launch_path))
            except Exception:
                expected_hash = ""
        else:
            expected_hash = ""
        expected_hashes[label.upper()] = expected_hash
        if not expected_hash or execution.get(f"bot_{label}_artifact_hash") != expected_hash:
            issues.append(f"official_formal_bot_{label}_sealed_identity_mismatch")

    isolation_receipt = (
        execution.get("bot_isolation")
        if isinstance(execution.get("bot_isolation"), dict)
        else {}
    )
    if isolation_receipt.get("schema_version") != 1:
        issues.append("official_formal_bot_isolation_schema_mismatch")
    if isolation_receipt.get("authority") != (
        "central-managed-executor-process-observation"
    ):
        issues.append("official_formal_bot_isolation_authority_mismatch")
    connections = (
        isolation_receipt.get("connections")
        if isinstance(isolation_receipt.get("connections"), dict)
        else {}
    )
    if set(connections) != {"A", "B"}:
        issues.append("official_formal_bot_isolation_connections_mismatch")

    profile = load_execution_profile()
    managed_identity = (
        profile.get("managed_executor")
        if isinstance(profile.get("managed_executor"), dict)
        else {}
    )
    seccomp = (
        managed_identity.get("seccomp")
        if isinstance(managed_identity.get("seccomp"), dict)
        else {}
    )
    expected_isolation = asdict(IsolationIdentity(
        policy_sha256=str(seccomp.get("policy_sha256") or ""),
        bpf_sha256=str(seccomp.get("bpf_sha256") or ""),
        bpf_size=int(seccomp.get("bpf_size", 0) or 0),
    ))
    expected_source_sha256 = str(
        ((managed_identity.get("source") or {}).get("sha256") or "")
    )
    instance_ids: list[str] = []
    for connection, launch in (("A", bot_a), ("B", bot_b)):
        row = connections.get(connection)
        if not isinstance(row, dict):
            issues.append(f"official_formal_bot_isolation_{connection}_missing")
            continue
        expected_scalars = {
            "connection": connection,
            "name": str(launch.get("name") or ""),
            "role": str(launch.get("role") or ""),
            "instance_id": str(launch.get("instance_id") or ""),
            "seat": str(launch.get("seat") or ""),
            "artifact_hash": expected_hashes.get(connection, ""),
            "managed_executor_source_sha256": expected_source_sha256,
        }
        for key, value in expected_scalars.items():
            if not value or row.get(key) != value:
                issues.append(
                    f"official_formal_bot_isolation_{connection}_{key}_mismatch"
                )
        if not _same_resolved_path(row.get("path"), str(launch.get("path") or "")):
            issues.append(f"official_formal_bot_isolation_{connection}_path_mismatch")
        if row.get("endpoint_lease") != {"consumed": True, "closed": True}:
            issues.append(
                f"official_formal_bot_isolation_{connection}_endpoint_lease_mismatch"
            )
        if row.get("execution_profile") != expected_profile:
            issues.append(
                f"official_formal_bot_isolation_{connection}_profile_mismatch"
            )
        isolation = row.get("isolation")
        if not isinstance(isolation, dict) or canonical_digest({
            "isolation": isolation,
        }) != canonical_digest({"isolation": expected_isolation}):
            issues.append(
                f"official_formal_bot_isolation_{connection}_policy_mismatch"
            )
        instance_ids.append(str(row.get("instance_id") or ""))
    if len(instance_ids) != 2 or len(set(instance_ids)) != 2:
        issues.append("official_formal_bot_isolation_instance_ids_not_unique")
    environment = receipt.get("environment") if isinstance(receipt.get("environment"), dict) else {}
    observed_profile = (
        environment.get("execution_profile")
        if isinstance(environment.get("execution_profile"), dict)
        else {}
    )
    if observed_profile.get("ok") is not True or observed_profile.get("issues"):
        issues.append("official_formal_execution_profile_not_verified")
    for key, value in expected_profile.items():
        if observed_profile.get(key) != value:
            issues.append(f"official_formal_observed_{key}_mismatch")
    return issues


def _log_target_reached(receipt: Any, target_hands: int) -> bool:
    if not isinstance(receipt, dict):
        return False
    return not round_completion_issues(receipt, target_hands)


def _same_resolved_path(left: Any, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(str(left)).expanduser().resolve() == Path(right).expanduser().resolve()
    except Exception:
        return False


def _full_evidence_artifact_issues(receipt: dict[str, Any]) -> list[str]:
    probe = receipt.get("wire_probe")
    if not isinstance(probe, dict) or not bool(probe.get("enabled")):
        return ["full_wire_probe_missing_or_disabled"]
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    issues: list[str] = []
    required_files = (
        "receipt",
        "platform_log",
        "bot_a_log",
        "bot_b_log",
        "bot_a_stdout",
        "bot_a_stderr",
        "bot_b_stdout",
        "bot_b_stderr",
        "wire_events",
        "replay_summary",
    )
    for key in required_files:
        value = artifacts.get(key)
        try:
            path = Path(str(value)) if value else None
            exists = bool(path) and path.is_file() and not path.is_symlink()
        except Exception:
            exists = False
        if not exists:
            issues.append(f"full_evidence_artifact_missing:{key}")
    for key in ("thp_files", "screenshots"):
        values = artifacts.get(key) or []
        if not isinstance(values, list):
            values = [values]
        try:
            retained = any(
                Path(str(value)).is_file() and not Path(str(value)).is_symlink()
                for value in values
                if value
            )
        except Exception:
            retained = False
        if not retained:
            issues.append(f"full_evidence_artifact_missing:{key}")
    return issues


def receipt_validation_issues(
    receipt: Any,
    spec: CertificationSpec,
    *,
    expected_kind: str | None = None,
    expected_index: int | None = None,
) -> list[str]:
    if not isinstance(receipt, dict):
        return [f"receipt_invalid_type:{type(receipt).__name__}"]
    issues: list[str] = []
    if receipt.get("passed") is not True:
        issues.append("receipt_not_passed")
    receipt_issues = receipt.get("issues") or []
    if isinstance(receipt_issues, list):
        issues.extend(str(issue) for issue in receipt_issues)
    elif receipt_issues:
        issues.append(f"receipt_issues_invalid_type:{type(receipt_issues).__name__}")
    try:
        receipt_target_hands = int(receipt.get("target_hands", 0) or 0)
    except Exception:
        receipt_target_hands = 0
    if receipt_target_hands != spec.target_hands:
        issues.append(f"target_hands_mismatch: receipt={receipt_target_hands} spec={spec.target_hands}")

    if expected_kind is not None and receipt.get("round_kind") != expected_kind:
        issues.append(
            f"round_kind_mismatch: receipt={receipt.get('round_kind')} expected={expected_kind}"
        )
    if expected_index is not None:
        try:
            actual_index = int(receipt.get("round_index", 0) or 0)
        except Exception:
            actual_index = 0
        if actual_index != expected_index:
            issues.append(
                f"round_index_mismatch: receipt={actual_index} expected={expected_index}"
            )

    bot_a = receipt.get("bot_a") if isinstance(receipt.get("bot_a"), dict) else {}
    bot_b = receipt.get("bot_b") if isinstance(receipt.get("bot_b"), dict) else {}
    if expected_kind == "self_play":
        if not _same_resolved_path(bot_a.get("path"), spec.candidate):
            issues.append("self_play_bot_a_candidate_identity_mismatch")
        if not _same_resolved_path(bot_b.get("path"), spec.candidate):
            issues.append("self_play_bot_b_candidate_identity_mismatch")
    elif expected_kind == "opponent":
        topology = round_topology(receipt)
        launches = {"A": bot_a, "B": bot_b}
        candidate_launches = [
            launches[label]
            for label, item in (topology.get("connections") or {}).items()
            if label in launches and item.get("role") == "candidate"
        ]
        opponent_launches = [
            launches[label]
            for label, item in (topology.get("connections") or {}).items()
            if label in launches and item.get("role") == "opponent"
        ]
        if len(candidate_launches) != 1 or not _same_resolved_path(
            (candidate_launches[0] if candidate_launches else {}).get("path"),
            spec.candidate,
        ):
            issues.append("opponent_round_candidate_identity_mismatch")
        if len(opponent_launches) != 1 or not _same_resolved_path(
            (opponent_launches[0] if opponent_launches else {}).get("path"),
            spec.opponent,
        ):
            issues.append("opponent_round_opponent_identity_mismatch")

    thp_hands = _max_thp_hands(receipt)
    if spec.mode == "full" or spec.target_hands >= 70:
        completion_issues = round_completion_issues(receipt, spec.target_hands)
        if completion_issues:
            issues.extend(completion_issues)
            summary = receipt.get("log_summary") or {}
            issues.append(
                "official_full_settlement_incomplete: "
                f"hands_started={summary.get('hands_started_min', 0)} "
                f"settlements={summary.get('settlements_min', 0)} "
                f"target={spec.target_hands}"
            )
        issues.extend(_full_evidence_artifact_issues(receipt))
        issues.extend(_formal_execution_issues(receipt))
        formal_thp_issues = _formal_thp_artifact_issues(
            receipt,
            expected_hands=spec.target_hands,
        )
        issues.extend(formal_thp_issues)
        if formal_thp_issues:
            issues.append(
                "thp_incomplete_for_full_certification: "
                f"exact_canonical_thp_required target={spec.target_hands}"
            )
            summary = receipt.get("log_summary") or {}
            try:
                hands_started = int(summary.get("hands_started_min", 0) or 0)
                settlements = int(summary.get("settlements_min", 0) or 0)
            except Exception:
                hands_started = 0
                settlements = 0
            if hands_started > 0 and hands_started < spec.target_hands:
                issues.append(
                    "official_full_round_incomplete_after_progress: "
                    f"hands_started={hands_started} settlements={settlements} "
                    f"target={spec.target_hands}"
                )
            elif hands_started == 0:
                issues.append(
                    "official_full_round_no_game_progress: "
                    f"target={spec.target_hands}"
                )
    elif thp_hands < spec.target_hands and not _log_target_reached(receipt, spec.target_hands):
        # Short smoke stops the official EXE before its natural 70-hand THP export.
        # Use bot/platform logs as smoke evidence; full certification still requires THP.
        issues.append(f"smoke_progress_incomplete: thp_hands={thp_hands} target={spec.target_hands}")
    return issues


def receipt_valid_for_spec(receipt: dict[str, Any], spec: CertificationSpec) -> bool:
    return not receipt_validation_issues(receipt, spec)


def report_validation_issues(report: Any, spec: CertificationSpec) -> list[str]:
    try:
        validate_spec(spec)
    except Exception as exc:
        return [f"invalid_certification_spec:{type(exc).__name__}:{str(exc)[:300]}"]
    if not isinstance(report, dict):
        return [f"report_invalid_type:{type(report).__name__}"]
    issues: list[str] = []
    if report.get("passed") is not True:
        issues.append("report_not_passed")
    report_issues = report.get("issues") or []
    if isinstance(report_issues, list):
        issues.extend(str(issue) for issue in report_issues)
    elif report_issues:
        issues.append(f"report_issues_invalid_type:{type(report_issues).__name__}")
    report_payload = report.get("report")
    if not isinstance(report_payload, dict):
        issues.append(f"report_payload_invalid_type:{type(report_payload).__name__}")
        report_payload = {}
    rounds = report_payload.get("rounds") or []
    if not isinstance(rounds, list):
        issues.append(f"report_rounds_invalid_type:{type(rounds).__name__}")
        rounds = []
    expected = spec.self_play_rounds + spec.opponent_rounds
    if len(rounds) != expected:
        issues.append(f"round_count_mismatch: rounds={len(rounds)} expected={expected}")
    if spec.mode == "full":
        from official_execution_profile import execution_profile_identity

        suite_execution = (
            report_payload.get("formal_execution")
            if isinstance(report_payload.get("formal_execution"), dict)
            else {}
        )
        if suite_execution.get("ok") is not True or suite_execution.get("issues"):
            issues.append("official_formal_suite_execution_not_verified")
        for key, value in execution_profile_identity().items():
            if suite_execution.get(key) != value:
                issues.append(f"official_formal_suite_{key}_mismatch")
        expected_rounds = [
            *(("self_play", index) for index in range(1, spec.self_play_rounds + 1)),
            *(("opponent", index) for index in range(1, spec.opponent_rounds + 1)),
        ]
    else:
        expected_rounds = [(None, None)] * expected
    for index, receipt in enumerate(rounds, start=1):
        expected_kind, expected_index = (
            expected_rounds[index - 1]
            if index <= len(expected_rounds)
            else (None, None)
        )
        receipt_issues = receipt_validation_issues(
            receipt,
            spec,
            expected_kind=expected_kind,
            expected_index=expected_index,
        )
        issues.extend(f"round_{index}: {issue}" for issue in receipt_issues)
    return issues


def report_valid_for_spec(report: dict[str, Any], spec: CertificationSpec) -> bool:
    return not report_validation_issues(report, spec)


def _job_envelope_report_issues(
    report: dict[str, Any],
    job_envelope: dict[str, Any] | None,
) -> list[str]:
    payload = report.get("report") if isinstance(report, dict) else None
    if not isinstance(payload, dict):
        return ["official_job_envelope_report_missing"]
    issues: list[str] = []
    if payload.get("job_envelope") != job_envelope:
        issues.append("official_job_envelope_suite_mismatch")
    for index, receipt in enumerate(payload.get("rounds") or [], start=1):
        if not isinstance(receipt, dict) or receipt.get("job_envelope") != job_envelope:
            issues.append(f"official_job_envelope_round_mismatch:{index}")
    return issues


def _cache_hit(
    spec: CertificationSpec,
    config: OfficialPlatformConfig | None = None,
    *,
    runner_provenance: str = PRODUCTION_RUNNER_PROVENANCE,
    test_only: bool = False,
) -> dict[str, Any] | None:
    identity = certification_identity(
        spec,
        config,
        runner_provenance=runner_provenance,
        test_only=test_only,
    )
    key = identity["identity_digest"]
    payload = _read_json(_cache_file(key))
    if (
        payload
        and payload.get("cache_key") == key
        and payload.get("identity") == identity
        and report_valid_for_spec(payload.get("result", {}), spec)
    ):
        return payload
    return None


def _write_cache(
    spec: CertificationSpec,
    result: dict[str, Any],
    identity: dict[str, Any],
) -> str:
    key = str(identity["identity_digest"])
    payload = {
        "cache_key": key,
        "created_at": now_iso(),
        "spec": spec_record(spec),
        "identity": identity,
        "result": result,
    }
    _write_json(_cache_file(key), payload)
    return key


def _status_path(label: str) -> Path:
    return status_dir() / f"{label}.json"


@contextmanager
def _status_lock(label: str):
    path = status_dir() / f"{label}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def _status_mode_rank(payload: dict[str, Any]) -> int:
    return {None: 0, "": 0, "smoke": 1, "compliance": 2, "full": 3}.get(
        payload.get("mode"),
        0,
    )


def _same_status_artifact(current: dict[str, Any], incoming: dict[str, Any]) -> bool:
    current_identity = current.get("certification_identity") or {}
    incoming_identity = incoming.get("certification_identity") or {}
    current_hash = str(current_identity.get("candidate_hash") or "")
    incoming_hash = str(incoming_identity.get("candidate_hash") or "")
    if current_hash and incoming_hash:
        return current_hash == incoming_hash
    # Missing identity is legacy metadata, not evidence that the bot changed.
    # Preserve a stronger existing result rather than allowing an old weak job
    # to erase it.
    return True


def _status_write_would_downgrade(
    current: dict[str, Any],
    incoming: dict[str, Any],
) -> bool:
    if not current or not _same_status_artifact(current, incoming):
        return False
    current_rank = _status_mode_rank(current)
    incoming_rank = _status_mode_rank(incoming)
    if incoming_rank < current_rank:
        return True
    if incoming_rank == current_rank:
        current_started = current.get("request_started_ns")
        incoming_started = incoming.get("request_started_ns")
        if (
            isinstance(current_started, int)
            and not isinstance(current_started, bool)
            and isinstance(incoming_started, int)
            and not isinstance(incoming_started, bool)
            and incoming_started < current_started
        ):
            return True
    terminal = {
        STATUS_SMOKE_PASS,
        STATUS_COMPLIANCE_PASS,
        STATUS_CERTIFIED,
        STATUS_FAILED,
        STATUS_INCONCLUSIVE,
    }
    return (
        incoming_rank == current_rank
        and incoming.get("status") == STATUS_PENDING
        and current.get("status") in terminal
    )


def _published_status(
    candidate: str | Path,
    *,
    require_published: bool = False,
) -> dict[str, Any] | None:
    path = published_certificate_path(candidate)
    record, attestation, container_issues = _load_certificate_container(path)
    if not isinstance(record, dict) or not isinstance(attestation, dict):
        return None
    evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
    status = {
        "bot": _safe_label(candidate),
        "status": STATUS_CERTIFIED,
        "status_label": STATUS_CERTIFIED,
        "mode": record.get("mode"),
        "updated_at": record.get("issued_at"),
        "policy_id": record.get("policy_id"),
        "cache_hit": False,
        "cache_key": record.get("cache_key"),
        "certification_identity": record.get("identity") or {},
        "test_only": bool((record.get("identity") or {}).get("test_only")),
        "authority_scope": (record.get("identity") or {}).get("authority_scope"),
        "opponent_selection": record.get("opponent_selection"),
        "official_job_envelope": record.get("job_envelope"),
        "official_evidence_archive": record.get("evidence_archive"),
        "summary": {},
        "issues": [],
        "official_evidence_path": evidence.get("path"),
        "official_evidence_summary": evidence.get("summary") or {},
        "official_deterministic_receipt": record.get("deterministic_receipt"),
        "certificate_schema_version": record.get("schema_version"),
        "certificate_digest": record.get("certificate_digest"),
        "certificate_path": str(path),
        "published_attestation_path": str(path),
        "published_attestation_digest": attestation.get("attestation_digest"),
        "certificate_signature": attestation.get("signature"),
        "certificate_signature_sha256": attestation.get("signature_sha256"),
    }
    validation = certificate_validation(
        status,
        candidate=candidate,
        require_published=require_published,
    )
    if container_issues or not validation.get("valid"):
        status.update({
            "status": STATUS_INCONCLUSIVE,
            "status_label": STATUS_INCONCLUSIVE,
            "issues": list(dict.fromkeys([
                *container_issues,
                *(validation.get("issues") or []),
            ])),
        })
    return status


def read_status(candidate: str | Path) -> dict[str, Any]:
    label = _safe_label(candidate)
    payload = _read_json(_status_path(label)) or {}
    published = _published_status(candidate, require_published=True)
    ledger_entry = None
    authoritative_context = bool(
        published
        or payload.get("mode") == "full"
        or payload.get("status") == STATUS_CERTIFIED
    )
    try:
        candidate_hash = hash_path(Path(candidate).expanduser().resolve())
        from official_verdict_ledger import latest_authoritative_verdict

        ledger = latest_authoritative_verdict(candidate_hash)
        if not ledger.get("valid"):
            if authoritative_context:
                base = published or payload or {"bot": label}
                return {
                    **base,
                    "status": STATUS_INCONCLUSIVE,
                    "status_label": STATUS_INCONCLUSIVE,
                    "mode": "full",
                    "issues": list(dict.fromkeys([
                        *(base.get("issues") or []),
                        *(ledger.get("issues") or []),
                    ])),
                    "certification_identity": (
                        base.get("certification_identity")
                        or {"candidate_hash": candidate_hash}
                    ),
                }
            ledger_entry = None
        else:
            ledger_entry = ledger.get("entry")
        if isinstance(ledger_entry, dict) and ledger_entry.get("outcome") == STATUS_FAILED:
            if (
                payload.get("status") == STATUS_FAILED
                and (payload.get("certification_identity") or {}).get("candidate_hash") == candidate_hash
            ):
                return {**payload, "official_verdict_ledger_entry": ledger_entry}
            return {
                "bot": label,
                "status": STATUS_FAILED,
                "status_label": STATUS_FAILED,
                "mode": "full",
                "updated_at": None,
                "policy_id": ledger_entry.get("policy_id"),
                "issues": ["signed_official_verdict_ledger_failure"],
                "certification_identity": {"candidate_hash": candidate_hash},
                "official_evidence_summary": {
                    "blocking": bool(ledger_entry.get("blocking")),
                    "inconclusive": False,
                    "classification": ledger_entry.get("classification"),
                },
                "official_verdict_ledger_entry": ledger_entry,
            }
    except Exception as exc:
        if authoritative_context:
            base = published or payload or {"bot": label}
            return {
                **base,
                "status": STATUS_INCONCLUSIVE,
                "status_label": STATUS_INCONCLUSIVE,
                "mode": "full",
                "issues": list(dict.fromkeys([
                    *(base.get("issues") or []),
                    f"official_verdict_ledger_validation_error:{type(exc).__name__}:{str(exc)[:160]}",
                ])),
                "certification_identity": (
                    base.get("certification_identity")
                    or {
                        "candidate_hash": (
                            candidate_hash if "candidate_hash" in locals() else ""
                        )
                    }
                ),
            }
        ledger_entry = None
    # A later deterministic run against the exact same artifact can revoke a
    # previously published pass. Mutable issue strings and summaries cannot.
    if (
        payload
        and published
        and published.get("status") == STATUS_CERTIFIED
        and not (
            isinstance(ledger_entry, dict)
            and ledger_entry.get("outcome") == STATUS_CERTIFIED
        )
    ):
        receipt_issues = _deterministic_status_receipt_issues(
            payload,
            candidate=candidate,
        )
        if not receipt_issues and official_compliance_verdict(payload).get("blocking"):
            return payload
    if published and published.get("status") == STATUS_CERTIFIED:
        return published
    if payload:
        return payload
    if published:
        return published
    return {
        "bot": label,
        "status": STATUS_UNCERTIFIED,
        "status_label": STATUS_UNCERTIFIED,
        "updated_at": None,
        "mode": None,
        "cache_hit": False,
        "summary": {},
        "issues": [],
    }


def write_status(candidate: str | Path, status: str, *, mode: CertificationMode | None = None, **extra: Any) -> dict[str, Any]:
    label = _safe_label(candidate)
    payload = {
        "bot": label,
        "status": status,
        "status_label": status,
        "mode": mode,
        "updated_at": now_iso(),
        **extra,
    }
    with _status_lock(label):
        current = _read_json(_status_path(label)) or {}
        if _status_write_would_downgrade(current, payload):
            return current
        _write_json(_status_path(label), payload)
        return payload


def record_local_pass(candidate: str | Path, *, source: str = "quality_gates") -> dict[str, Any]:
    current = read_status(candidate)
    if current.get("status") in {
        STATUS_SMOKE_PASS,
        STATUS_COMPLIANCE_PASS,
        STATUS_PENDING,
        STATUS_CERTIFIED,
        STATUS_INCONCLUSIVE,
    }:
        return current
    if current.get("status") == STATUS_FAILED and official_failure_blocks_parent(current):
        return current
    return write_status(candidate, STATUS_LOCAL_PASS, source=source, issues=[])


def _official_issue_strings(status: dict[str, Any]) -> list[str]:
    return [str(issue) for issue in (status.get("issues") or [])]


def _deterministic_status_projection(deterministic: dict[str, Any]) -> dict[str, Any]:
    """Return the deterministic fields allowed to carry gate authority."""
    return {
        "passed": bool(deterministic.get("passed")),
        "classification": str(deterministic.get("classification") or ""),
        "blocking": bool(deterministic.get("blocking")),
        "inconclusive": bool(deterministic.get("inconclusive")),
        "violation": bool(deterministic.get("violation")),
        "candidate_verdict": str(deterministic.get("candidate_verdict") or ""),
        "rounds_requested": deterministic.get("rounds_requested"),
        "rounds_run": deterministic.get("rounds_run"),
        "target_hands": deterministic.get("target_hands"),
        "issues": [str(item) for item in (deterministic.get("issues") or [])],
    }


def _build_deterministic_status_receipt(
    spec: CertificationSpec,
    identity: dict[str, Any],
    evidence_path: Path,
    deterministic: dict[str, Any],
    cache_key_value: str,
    archive: dict[str, Any] | None = None,
) -> dict[str, Any]:
    archive = archive if isinstance(archive, dict) else {}
    payload = {
        "schema_version": DETERMINISTIC_STATUS_RECEIPT_SCHEMA_VERSION,
        "candidate_label": _safe_label(spec.candidate),
        "candidate_hash": str(identity.get("candidate_hash") or ""),
        "policy_id": spec.policy_id,
        "mode": spec.mode,
        "cache_key": cache_key_value,
        "evidence_sha256": _file_sha256(evidence_path),
        "archive_sha256": str(archive.get("archive_sha256") or ""),
        "archive_manifest_digest": str(archive.get("manifest_digest") or ""),
        "verdict": _deterministic_status_projection(deterministic),
        "strength_evaluation": "not_applicable",
    }
    payload["receipt_digest"] = canonical_digest(payload)
    return payload


def _deterministic_status_receipt_issues(
    status: dict[str, Any],
    *,
    candidate: str | Path | None = None,
) -> list[str]:
    receipt = status.get("official_deterministic_status_receipt")
    if not isinstance(receipt, dict):
        return ["official_deterministic_status_receipt_missing"]
    issues: list[str] = []
    if receipt.get("schema_version") != DETERMINISTIC_STATUS_RECEIPT_SCHEMA_VERSION:
        issues.append("official_deterministic_status_receipt_schema_mismatch")
    digest = str(receipt.get("receipt_digest") or "")
    expected_digest = canonical_digest({
        key: value for key, value in receipt.items() if key != "receipt_digest"
    })
    if not digest or digest != expected_digest:
        issues.append("official_deterministic_status_receipt_digest_mismatch")
    expected_label = _safe_label(candidate) if candidate is not None else str(status.get("bot") or "")
    if not expected_label or receipt.get("candidate_label") != expected_label:
        issues.append("official_deterministic_status_candidate_label_mismatch")
    if status.get("bot") and receipt.get("candidate_label") != status.get("bot"):
        issues.append("official_deterministic_status_bot_mismatch")
    identity = status.get("certification_identity") or {}
    candidate_hash = str(identity.get("candidate_hash") or "")
    if not candidate_hash or receipt.get("candidate_hash") != candidate_hash:
        issues.append("official_deterministic_status_candidate_hash_mismatch")
    if receipt.get("policy_id") != status.get("policy_id"):
        issues.append("official_deterministic_status_policy_mismatch")
    if receipt.get("mode") != status.get("mode"):
        issues.append("official_deterministic_status_mode_mismatch")
    if receipt.get("cache_key") != status.get("cache_key"):
        issues.append("official_deterministic_status_cache_key_mismatch")
    summary = status.get("official_evidence_summary")
    summary = summary if isinstance(summary, dict) else {}
    verdict = receipt.get("verdict")
    if not isinstance(verdict, dict):
        issues.append("official_deterministic_status_verdict_missing")
    else:
        for key in ("classification", "blocking", "inconclusive", "violation"):
            if verdict.get(key) != summary.get(key):
                issues.append(f"official_deterministic_status_{key}_mismatch")
    evidence_path_value = status.get("official_evidence_path")
    try:
        evidence_path = Path(str(evidence_path_value or ""))
        if not evidence_path_value or evidence_path.is_symlink() or not evidence_path.is_file():
            issues.append("official_deterministic_status_evidence_missing")
        elif _file_sha256(evidence_path) != receipt.get("evidence_sha256"):
            issues.append("official_deterministic_status_evidence_digest_mismatch")
    except Exception as exc:
        issues.append(
            f"official_deterministic_status_evidence_error:{type(exc).__name__}:{str(exc)[:120]}"
        )
    archive = status.get("official_evidence_archive")
    archive = archive if isinstance(archive, dict) else {}
    if receipt.get("archive_sha256") != str(archive.get("archive_sha256") or ""):
        issues.append("official_deterministic_status_archive_mismatch")
    if receipt.get("archive_manifest_digest") != str(archive.get("manifest_digest") or ""):
        issues.append("official_deterministic_status_archive_manifest_mismatch")
    if receipt.get("mode") == "full" and isinstance(verdict, dict) and verdict.get("blocking"):
        archive_validation = validate_evidence_archive(
            archive,
            expected_evidence_sha256=str(receipt.get("evidence_sha256") or ""),
        )
        if not archive_validation.get("valid"):
            issues.extend(archive_validation.get("issues") or ["official_deterministic_status_archive_invalid"])
    if candidate is not None:
        try:
            if hash_path(Path(candidate).expanduser().resolve()) != candidate_hash:
                issues.append("official_deterministic_status_live_artifact_mismatch")
        except Exception as exc:
            issues.append(
                f"official_deterministic_status_artifact_error:{type(exc).__name__}:{str(exc)[:120]}"
            )
    if receipt.get("strength_evaluation") != "not_applicable":
        issues.append("official_deterministic_status_strength_scope_invalid")
    return list(dict.fromkeys(issues))


def _issue_has_marker(issue: str, markers: tuple[str, ...]) -> bool:
    lower = issue.lower()
    return any(marker in lower for marker in markers)


def _issues_have_protocol_violation(issues: list[str]) -> bool:
    return any(_issue_has_marker(issue, PARENT_BLOCKING_FAILURE_MARKERS) for issue in issues)


def official_compliance_verdict(status: dict[str, Any]) -> dict[str, Any]:
    """Classify official-platform evidence as a compliance oracle.

    The Windows EXE is used to catch explicit protocol/illegal-action violations.
    Harness problems such as Wine startup, occupied ports, missing THP export, or
    progress timeouts are evidence gaps, not bot-compliance failures.
    """
    status_value = str(status.get("status") or "")
    issues = _official_issue_strings(status)
    evidence_summary = status.get("official_evidence_summary") if isinstance(status, dict) else {}
    evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
    evidence_class = str(evidence_summary.get("classification") or "")
    if bool(evidence_summary.get("inconclusive")) and status_value in {
        STATUS_CERTIFIED,
        STATUS_COMPLIANCE_PASS,
        STATUS_SMOKE_PASS,
    }:
        return {
            "ok": True,
            "blocking": False,
            "classification": evidence_class or "inconclusive",
            "inconclusive": True,
            "violation": False,
            "issues": issues,
            "inconclusive_issues": issues,
            "official_evidence_summary": evidence_summary,
        }
    if status_value == STATUS_UNCERTIFIED:
        return {
            "ok": True,
            "blocking": False,
            "classification": "uncertified",
            "inconclusive": True,
            "violation": False,
            "issues": issues,
            "inconclusive_issues": issues,
        }
    if status_value == STATUS_LOCAL_PASS:
        return {
            "ok": True,
            "blocking": False,
            "classification": "local_pass",
            "inconclusive": False,
            "violation": False,
            "issues": issues,
        }
    if status_value == STATUS_INCONCLUSIVE:
        return {
            "ok": True,
            "blocking": False,
            "classification": "inconclusive",
            "inconclusive": True,
            "violation": False,
            "issues": issues,
            "inconclusive_issues": issues,
        }
    if status_value != STATUS_FAILED:
        return {
            "ok": True,
            "blocking": False,
            "classification": "passed_or_pending",
            "inconclusive": False,
            "violation": False,
            "issues": issues,
        }

    identity = status.get("certification_identity")
    identity = identity if isinstance(identity, dict) else {}
    candidate_hash = str(identity.get("candidate_hash") or "")
    if candidate_hash:
        try:
            from official_verdict_ledger import latest_authoritative_verdict

            ledger = latest_authoritative_verdict(candidate_hash)
            entry = ledger.get("entry") if ledger.get("valid") else None
            if (
                isinstance(entry, dict)
                and entry.get("outcome") == STATUS_FAILED
                and entry.get("blocking") is True
            ):
                classification = str(entry.get("classification") or "deterministic_blocking")
                return {
                    "ok": False,
                    "blocking": True,
                    "classification": classification,
                    "inconclusive": False,
                    "violation": classification == "protocol",
                    "issues": issues,
                    "violation_issues": issues,
                    "official_evidence_summary": evidence_summary,
                    "signed_verdict_ledger_valid": True,
                }
        except Exception:
            pass

    receipt_issues = _deterministic_status_receipt_issues(status)
    receipt = status.get("official_deterministic_status_receipt") or {}
    receipt_verdict = receipt.get("verdict") if isinstance(receipt, dict) else {}
    receipt_verdict = receipt_verdict if isinstance(receipt_verdict, dict) else {}
    if not receipt_issues and bool(receipt_verdict.get("blocking")) and not bool(receipt_verdict.get("inconclusive")):
        verdict_class = str(receipt_verdict.get("classification") or "deterministic_blocking")
        if verdict_class == "protocol":
            verdict_class = "protocol_violation"
        elif verdict_class == "obvious_decision_error":
            verdict_class = "official_full_incomplete"
        return {
            "ok": False,
            "blocking": True,
            "classification": verdict_class,
            "inconclusive": False,
            "violation": bool(receipt_verdict.get("violation")),
            "issues": issues,
            "violation_issues": list(receipt_verdict.get("issues") or []),
            "official_evidence_summary": evidence_summary,
            "deterministic_receipt_valid": True,
        }
    inconclusive_issues = [
        issue for issue in issues
        if _issue_has_marker(issue, COMPLIANCE_INCONCLUSIVE_FAILURE_MARKERS)
    ]
    return {
        "ok": True,
        "blocking": False,
        "classification": "inconclusive",
        "inconclusive": True,
        "violation": False,
        "issues": issues,
        "inconclusive_issues": inconclusive_issues or issues or receipt_issues,
        "deterministic_receipt_issues": receipt_issues,
    }


def official_failure_blocks_parent(status: dict[str, Any]) -> bool:
    return bool(official_compliance_verdict(status).get("blocking"))


def _certificate_payload_digest(record: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in record.items()
        if key not in {"certificate_digest", "certificate_path"}
    }
    return canonical_digest(payload)


def _build_deterministic_receipt(
    spec: CertificationSpec,
    evidence: dict[str, Any],
    evidence_path: Path,
    archive: dict[str, Any],
) -> dict[str, Any]:
    deterministic = evidence.get("deterministic") or {}
    rounds: list[dict[str, Any]] = []
    for item in evidence.get("rounds") or []:
        if not isinstance(item, dict):
            continue
        attribution = item.get("attribution") or {}
        log_summary = item.get("log_summary") or {}
        thp_summaries = item.get("thp_summaries") or []
        canonical_thp = item.get("canonical_thp") if isinstance(item.get("canonical_thp"), dict) else {}
        completion = (
            item.get("completion_evidence")
            if isinstance(item.get("completion_evidence"), dict)
            else {}
        )
        rounds.append({
            "round_kind": item.get("round_kind"),
            "round_index": item.get("round_index"),
            "target_hands": item.get("target_hands"),
            "passed": bool(item.get("passed")),
            "classification": item.get("classification"),
            "candidate_verdict": attribution.get("candidate_verdict"),
            "candidate_blocking": bool(attribution.get("candidate_blocking")),
            "countable": bool(attribution.get("countable")),
            "hands_started": int(log_summary.get("hands_started_min", 0) or 0),
            "settlements": int(log_summary.get("settlements_min", 0) or 0),
            "completed_hands": int(
                completion.get("completed_hands")
                or min(
                    int(log_summary.get("hands_started_min", 0) or 0),
                    int(log_summary.get("settlements_min", 0) or 0),
                )
            ),
            "thp_hands": int(canonical_thp.get("hand_records", 0) or 0),
            "thp_sha256": str(canonical_thp.get("sha256") or ""),
            "completion_kind": str(completion.get("kind") or "paired-tcp-settlements"),
            "completion_evidence_digest": str(completion.get("evidence_digest") or ""),
            "issue_count": len(item.get("issues") or []),
        })
    payload = {
        "schema_version": DETERMINISTIC_RECEIPT_SCHEMA_VERSION,
        "policy_id": spec.policy_id,
        "spec": {
            "self_play_rounds": spec.self_play_rounds,
            "opponent_rounds": spec.opponent_rounds,
            "target_hands": spec.target_hands,
        },
        "verdict": {
            "passed": bool(deterministic.get("passed")),
            "classification": deterministic.get("classification"),
            "blocking": bool(deterministic.get("blocking")),
            "inconclusive": bool(deterministic.get("inconclusive")),
            "candidate_verdict": deterministic.get("candidate_verdict"),
            "rounds_requested": (
                deterministic.get("rounds_requested")
                or spec.self_play_rounds + spec.opponent_rounds
            ),
            "rounds_run": (
                deterministic.get("rounds_run")
                or len(evidence.get("rounds") or [])
            ),
            "target_hands": deterministic.get("target_hands") or spec.target_hands,
            "issue_count": len(deterministic.get("issues") or []),
        },
        "rounds": rounds,
        "evidence_sha256": _file_sha256(evidence_path),
        "archive_sha256": archive.get("archive_sha256"),
        "archive_manifest_digest": archive.get("manifest_digest"),
        "strength_evaluation": "not_applicable",
    }
    payload["receipt_digest"] = canonical_digest(payload)
    return payload


def _deterministic_receipt_issues(
    receipt: Any,
    spec: CertificationSpec,
    *,
    evidence_manifest: dict[str, Any],
    archive_receipt: dict[str, Any],
) -> list[str]:
    if not isinstance(receipt, dict):
        return ["certificate_deterministic_receipt_missing"]
    issues: list[str] = []
    if receipt.get("schema_version") != DETERMINISTIC_RECEIPT_SCHEMA_VERSION:
        issues.append("certificate_deterministic_receipt_schema_mismatch")
    digest = str(receipt.get("receipt_digest") or "")
    expected_digest = canonical_digest({
        key: value for key, value in receipt.items() if key != "receipt_digest"
    })
    if digest != expected_digest:
        issues.append("certificate_deterministic_receipt_digest_mismatch")
    if receipt.get("policy_id") != spec.policy_id:
        issues.append("certificate_deterministic_receipt_policy_mismatch")
    if receipt.get("spec") != {
        "self_play_rounds": spec.self_play_rounds,
        "opponent_rounds": spec.opponent_rounds,
        "target_hands": spec.target_hands,
    }:
        issues.append("certificate_deterministic_receipt_spec_mismatch")
    verdict = receipt.get("verdict") if isinstance(receipt.get("verdict"), dict) else {}
    if verdict != {
        "passed": True,
        "classification": "pass",
        "blocking": False,
        "inconclusive": False,
        "candidate_verdict": "pass",
        "rounds_requested": spec.self_play_rounds + spec.opponent_rounds,
        "rounds_run": spec.self_play_rounds + spec.opponent_rounds,
        "target_hands": spec.target_hands,
        "issue_count": 0,
    }:
        issues.append("certificate_deterministic_verdict_not_full_pass")
    rounds = receipt.get("rounds") if isinstance(receipt.get("rounds"), list) else []
    expected_rounds = [
        ("self_play", index) for index in range(1, spec.self_play_rounds + 1)
    ] + [
        ("opponent", index) for index in range(1, spec.opponent_rounds + 1)
    ]
    actual_rounds: list[tuple[str, int]] = []
    for item in rounds:
        if not isinstance(item, dict):
            issues.append("certificate_deterministic_round_invalid")
            continue
        try:
            round_index = int(item.get("round_index"))
            target_hands = int(item.get("target_hands", 0) or 0)
            thp_hands = int(item.get("thp_hands", 0) or 0)
            hands_started = int(item.get("hands_started", 0) or 0)
            settlements = int(item.get("settlements", 0) or 0)
            completed_hands = int(item.get("completed_hands", 0) or 0)
            issue_count = int(item.get("issue_count", -1) or 0)
        except (TypeError, ValueError, OverflowError):
            issues.append("certificate_deterministic_round_identity_invalid")
            continue
        actual_rounds.append((str(item.get("round_kind")), round_index))
        paired_completion = (
            hands_started >= target_hands
            and settlements >= target_hands
            and item.get("completion_kind") == "paired-tcp-settlements"
        )
        thp_terminal_completion = (
            target_hands == 70
            and hands_started == 70
            and settlements == 69
            and item.get("completion_kind") == "official-thp-terminal-settlement"
            and len(str(item.get("completion_evidence_digest") or "")) == 64
        )
        if not (
            item.get("passed") is True
            and item.get("classification") == "pass"
            and item.get("candidate_verdict") == "pass"
            and item.get("candidate_blocking") is False
            and item.get("countable") is True
            and target_hands == spec.target_hands
            and thp_hands == spec.target_hands
            and completed_hands == spec.target_hands
            and len(str(item.get("thp_sha256") or "")) == 64
            and (paired_completion or thp_terminal_completion)
            and issue_count == 0
        ):
            issues.append(
                f"certificate_deterministic_round_not_passed:{item.get('round_kind')}:{item.get('round_index')}"
            )
    if actual_rounds != expected_rounds:
        issues.append("certificate_deterministic_round_set_mismatch")
    if receipt.get("evidence_sha256") != evidence_manifest.get("sha256"):
        issues.append("certificate_deterministic_evidence_digest_mismatch")
    if receipt.get("archive_sha256") != archive_receipt.get("archive_sha256"):
        issues.append("certificate_deterministic_archive_digest_mismatch")
    if receipt.get("archive_manifest_digest") != archive_receipt.get("manifest_digest"):
        issues.append("certificate_deterministic_archive_manifest_mismatch")
    if receipt.get("strength_evaluation") != "not_applicable":
        issues.append("certificate_deterministic_strength_scope_invalid")
    return issues


def _spec_from_mapping(data: dict[str, Any]) -> CertificationSpec:
    retired = sorted(RETIRED_BOOTSTRAP_SPEC_FIELDS.intersection(data))
    if retired:
        raise ValueError(
            "retired signed-ledger bootstrap spec fields are forbidden: "
            + ", ".join(retired)
        )
    mode = str(data.get("mode") or "")
    spec = CertificationSpec(
        mode=mode,
        policy_id=str(
            data.get("policy_id")
            or (FULL_POLICY_ID if mode == "full" else f"official-{mode}-v1")
        ),
        candidate=str(data.get("candidate") or ""),
        opponent=str(data.get("opponent")) if data.get("opponent") else None,
        self_play_rounds=int(data.get("self_play_rounds", 0) or 0),
        opponent_rounds=int(data.get("opponent_rounds", 0) or 0),
        target_hands=int(data.get("target_hands", 0) or 0),
        round_timeout_sec=float(data.get("round_timeout_sec", 0.0) or 0.0),
        no_progress_timeout_sec=float(data.get("no_progress_timeout_sec", 0.0) or 0.0),
        bootstrap_control_id=(
            str(data.get("bootstrap_control_id")).strip()
            if data.get("bootstrap_control_id") is not None
            else None
        ),
    )
    validate_spec(spec)
    return spec


def _config_for_spec(
    spec: CertificationSpec,
    config: OfficialPlatformConfig | None = None,
) -> OfficialPlatformConfig:
    return _copy_config(
        config or OfficialPlatformConfig(),
        round_timeout_sec=spec.round_timeout_sec,
        no_progress_timeout_sec=spec.no_progress_timeout_sec,
        results_dir=certification_root() / spec.mode,
    )


def _identity_integrity_issues(identity: Any, spec: CertificationSpec) -> list[str]:
    if not isinstance(identity, dict):
        return ["certificate_identity_missing"]
    issues: list[str] = []
    identity_payload = {
        key: value
        for key, value in identity.items()
        if key != "identity_digest"
    }
    if identity.get("identity_digest") != canonical_digest(identity_payload):
        issues.append("certificate_identity_digest_mismatch")
    platform = identity.get("platform")
    if not isinstance(platform, dict):
        issues.append("certificate_platform_identity_missing")
    elif identity.get("platform_fingerprint") != canonical_digest(platform):
        issues.append("certificate_platform_fingerprint_mismatch")
    if identity.get("policy_id") != spec.policy_id:
        issues.append("certificate_identity_policy_mismatch")
    if identity.get("spec") != spec_record(spec):
        issues.append("certificate_identity_spec_mismatch")
    return issues


def _opponent_selection_issues(
    selection: Any,
    spec: CertificationSpec,
    identity: dict[str, Any],
    *,
    allow_consumed_bootstrap: bool = False,
    _validated_ledger_entries: list[dict[str, Any]] | None = None,
) -> list[str]:
    if spec.mode != "full":
        return []
    if not isinstance(selection, dict) or selection.get("selected") is not True:
        return ["certificate_official_opponent_selection_missing"]
    opponent = selection.get("opponent")
    if not isinstance(opponent, dict) or opponent.get("eligible") is not True:
        return ["certificate_official_opponent_selection_invalid"]
    issues: list[str] = []
    try:
        selected_path = Path(str(opponent.get("path") or "")).resolve()
        expected_path = Path(str(spec.opponent or "")).resolve()
        if selected_path != expected_path:
            issues.append("certificate_official_opponent_path_mismatch")
    except Exception:
        issues.append("certificate_official_opponent_path_invalid")
    if str(opponent.get("artifact_hash") or "") != str(identity.get("opponent_hash") or ""):
        issues.append("certificate_official_opponent_hash_mismatch")
    reason = opponent.get("reason")
    if spec.bootstrap_control_id is not None:
        # The first strict run uses current system-owned typed-policy bytes,
        # never an archived bot.  Every selection/receipt field is revalidated.
        if selection.get("bootstrap_control_id") != spec.bootstrap_control_id:
            issues.append("certificate_bootstrap_control_id_mismatch")
        if reason != "first_strict_control_bootstrap":
            issues.append("certificate_bootstrap_control_reason_invalid")
        try:
            if _validated_ledger_entries is None:
                from official_bootstrap import (
                    validate_first_strict_control_selection,
                )

                validation = validate_first_strict_control_selection(
                    selection,
                    spec.bootstrap_control_id,
                    spec.candidate,
                    allow_consumed=allow_consumed_bootstrap,
                    allow_published=allow_consumed_bootstrap,
                )
            else:
                from official_bootstrap import (
                    validate_first_strict_control_selection_from_entries,
                )

                validation = (
                    validate_first_strict_control_selection_from_entries(
                        selection,
                        spec.bootstrap_control_id,
                        spec.candidate,
                        _validated_ledger_entries,
                        allow_consumed=allow_consumed_bootstrap,
                        allow_published=allow_consumed_bootstrap,
                    )
                )
            if not validation.get("valid"):
                issues.extend(
                    f"certificate_{item}"
                    for item in (
                        validation.get("issues")
                        or ["bootstrap_control_selection_invalid"]
                    )
                )
        except Exception as exc:
            issues.append(
                "certificate_bootstrap_control_validation_error:"
                f"{type(exc).__name__}:{str(exc)[:160]}"
            )
        return list(dict.fromkeys(issues))

    if selection.get("bootstrap_control_id") is not None:
        issues.append("certificate_bootstrap_control_unexpected")
    # A full EXE certificate is the only production authorization for an
    # official opponent.  Content-bound migration grants remain available for
    # parent/rating-pool history, but must never validate a formal opponent
    # receipt (including a resumed durable job).
    if reason != "official_certified":
        issues.append("certificate_official_opponent_reason_invalid")
    receipt = opponent.get("eligibility_receipt")
    if not isinstance(receipt, dict):
        issues.append("certificate_official_opponent_eligibility_receipt_missing")
        return issues
    receipt_payload = {
        key: value
        for key, value in receipt.items()
        if key != "receipt_digest"
    }
    if receipt.get("receipt_digest") != canonical_digest(receipt_payload):
        issues.append("certificate_official_opponent_eligibility_receipt_digest_mismatch")
    if receipt.get("schema_version") != OPPONENT_ELIGIBILITY_RECEIPT_SCHEMA_VERSION:
        issues.append("certificate_official_opponent_eligibility_receipt_schema_mismatch")
    if receipt.get("role") != "official_opponent":
        issues.append("certificate_official_opponent_eligibility_receipt_role_mismatch")
    if str(receipt.get("bot") or "") != str(opponent.get("bot") or ""):
        issues.append("certificate_official_opponent_eligibility_receipt_bot_mismatch")
    if str(receipt.get("artifact_hash") or "") != str(opponent.get("artifact_hash") or ""):
        issues.append("certificate_official_opponent_eligibility_receipt_hash_mismatch")
    expected_kind = "official_full_certificate"
    if receipt.get("kind") != expected_kind:
        issues.append("certificate_official_opponent_eligibility_receipt_kind_mismatch")
    if receipt.get("policy_id") != FULL_POLICY_ID:
        issues.append("certificate_official_opponent_certificate_policy_mismatch")
    certificate_digest = str(receipt.get("certificate_digest") or "")
    if len(certificate_digest) != 64 or any(
        ch not in "0123456789abcdef" for ch in certificate_digest.lower()
    ):
        issues.append("certificate_official_opponent_certificate_digest_invalid")
    return issues


def _validate_portable_file_manifest(
    manifest: Any,
    *,
    label: str,
) -> tuple[Path | None, list[str], bool]:
    """Validate retained bytes when present, otherwise validate the hash receipt."""
    path, issues = _validate_certificate_file_manifest(manifest, label=label)
    if not issues:
        return path, [], True
    if not isinstance(manifest, dict):
        return None, issues, False
    only_unretained = issues == [f"certificate_{label}_missing"]
    digest = str(manifest.get("sha256") or "")
    size = manifest.get("size_bytes")
    digest_ok = len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest.lower())
    try:
        size_ok = int(size) >= 0
    except (TypeError, ValueError):
        size_ok = False
    if only_unretained and digest_ok and size_ok:
        return None, [], False
    return None, issues, False


def _validate_published_attestation_at_tag(
    candidate_path: Path,
    published_identity: dict[str, Any],
    expected_certificate_digest: str,
) -> list[str]:
    path = published_certificate_path(candidate_path)
    record, attestation, issues = _load_certificate_container(path)
    if not isinstance(record, dict) or not isinstance(attestation, dict):
        return [*issues, "published_attestation_missing"]
    if attestation.get("bot") != candidate_path.name:
        issues.append("published_attestation_bot_mismatch")
    if record.get("certificate_digest") != expected_certificate_digest:
        issues.append("published_attestation_certificate_mismatch")
    try:
        relative = path.relative_to(ROOT).as_posix()
    except ValueError:
        return [*issues, "published_attestation_outside_repository"]
    tag = str(published_identity.get("tag") or "")
    if not tag:
        return [*issues, "published_attestation_tag_missing"]
    try:
        result = subprocess.run(
            ["git", "show", f"{tag}:{relative}"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        return [
            *issues,
            f"published_attestation_git_read_error:{type(exc).__name__}:{str(exc)[:160]}",
        ]
    if result.returncode != 0:
        return [*issues, "completion_tag_missing_published_attestation"]
    try:
        tagged = json.loads(result.stdout)
    except Exception as exc:
        return [
            *issues,
            f"completion_tag_attestation_invalid_json:{type(exc).__name__}:{str(exc)[:120]}",
        ]
    if tagged != attestation:
        issues.append("working_attestation_differs_from_completion_tag")
    return issues


def certificate_validation(
    status: dict[str, Any],
    *,
    candidate: str | Path | None = None,
    config: OfficialPlatformConfig | None = None,
    require_published: bool = False,
    _skip_ledger_check: bool = False,
    _validated_ledger_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    path_value = status.get("certificate_path")
    record_path = Path(str(path_value)) if path_value else Path()
    record, attestation, container_issues = (
        _load_certificate_container(record_path)
        if path_value
        else (None, None, ["content_bound_certificate_missing"])
    )
    issues.extend(container_issues)
    if not isinstance(record, dict):
        return {"valid": False, "issues": list(dict.fromkeys(issues))}
    portable = isinstance(attestation, dict)
    if record.get("schema_version") != CERTIFICATE_SCHEMA_VERSION:
        issues.append("certificate_schema_version_mismatch")
    if record.get("kind") != "official-exe-compliance-certificate":
        issues.append("certificate_kind_mismatch")
    digest = str(record.get("certificate_digest") or "")
    if not digest or digest != _certificate_payload_digest(record):
        issues.append("certificate_digest_mismatch")
    if digest != str(status.get("certificate_digest") or ""):
        issues.append("status_certificate_digest_mismatch")
    issuer = record.get("issuer") if isinstance(record.get("issuer"), dict) else {}
    from official_certificate_signing import (
        SIGNATURE_NAMESPACE,
        SIGNER_PRINCIPAL,
        verify_certificate_signature,
    )

    if issuer.get("principal") != SIGNER_PRINCIPAL:
        issues.append("official_certificate_issuer_principal_mismatch")
    if issuer.get("namespace") != SIGNATURE_NAMESPACE:
        issues.append("official_certificate_issuer_namespace_mismatch")
    signature = ""
    if portable:
        signature = str((attestation or {}).get("signature") or "")
    else:
        signature_path_value = status.get("certificate_signature_path")
        signature_path = (
            Path(str(signature_path_value))
            if signature_path_value
            else record_path.with_suffix(".sig")
        )
        try:
            if signature_path.is_symlink() or not signature_path.is_file():
                issues.append("official_certificate_signature_missing")
            else:
                signature = signature_path.read_text(encoding="utf-8")
                expected_signature_sha = str(status.get("certificate_signature_sha256") or "")
                if expected_signature_sha and _file_sha256(signature_path) != expected_signature_sha:
                    issues.append("official_certificate_signature_sha256_mismatch")
        except OSError as exc:
            issues.append(f"official_certificate_signature_read_error:{type(exc).__name__}")
    signature_validation = verify_certificate_signature(record, signature)
    issues.extend(signature_validation.get("issues") or [])
    if (
        signature_validation.get("valid")
        and issuer.get("key_fingerprint")
        != signature_validation.get("key_fingerprint")
    ):
        issues.append("official_certificate_issuer_fingerprint_mismatch")
    try:
        spec = _spec_from_mapping(record.get("spec") or {})
    except Exception as exc:
        return {
            "valid": False,
            "issues": list(dict.fromkeys([
                *issues,
                f"certificate_spec_invalid:{type(exc).__name__}:{str(exc)[:200]}",
            ])),
        }
    spec_candidate_label = _safe_label(spec.candidate)
    requested_candidate_label = _safe_label(candidate) if candidate is not None else spec_candidate_label
    if record.get("candidate_label") != spec_candidate_label:
        issues.append("certificate_candidate_label_missing_or_mismatch")
    if requested_candidate_label != spec_candidate_label:
        issues.append("certificate_candidate_version_mismatch")
    if portable and (attestation or {}).get("bot") != requested_candidate_label:
        issues.append("published_attestation_bot_mismatch")
    record_identity = record.get("identity") or {}
    if record_identity.get("runner_provenance") != PRODUCTION_RUNNER_PROVENANCE:
        issues.append("certificate_runner_provenance_not_production_official_exe")
    if record_identity.get("authority_scope") != "production":
        issues.append("certificate_authority_scope_not_production")
    if record_identity.get("test_only") is not False:
        issues.append("certificate_test_only_authority_forbidden")
    if status.get("test_only") is True:
        issues.append("status_test_only_authority_forbidden")
    if portable:
        issues.extend(_identity_integrity_issues(record_identity, spec))
        current_identity = record_identity
    else:
        current_identity = certification_identity(spec, _config_for_spec(spec, config))
        if record_identity != current_identity:
            issues.append("certificate_identity_stale")
    status_identity = status.get("certification_identity") or {}
    if status_identity != current_identity:
        issues.append("status_identity_stale")
    issues.extend(
        _opponent_selection_issues(
            record.get("opponent_selection"),
            spec,
            current_identity,
            allow_consumed_bootstrap=not _skip_ledger_check,
            _validated_ledger_entries=_validated_ledger_entries,
        )
    )
    if spec.mode == "full":
        from official_job_envelope import job_envelope_issues

        issues.extend(job_envelope_issues(
            record.get("job_envelope"),
            expected_candidate_hash=str(current_identity.get("candidate_hash") or ""),
            expected_opponent_hash=str(current_identity.get("opponent_hash") or ""),
        ))
    evidence_record = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
    if portable:
        archive_validation = validate_evidence_archive_receipt(
            record.get("evidence_archive"),
            expected_evidence_sha256=str(evidence_record.get("sha256") or ""),
        )
        retained_archive_validation = validate_evidence_archive(
            record.get("evidence_archive"),
            expected_evidence_sha256=str(evidence_record.get("sha256") or ""),
        )
    else:
        archive_validation = validate_evidence_archive(
            record.get("evidence_archive"),
            expected_evidence_sha256=str(evidence_record.get("sha256") or ""),
        )
        retained_archive_validation = archive_validation
    if spec.mode == "full":
        issues.extend(archive_validation.get("issues") or [])
    candidate_path = Path(candidate).expanduser().resolve() if candidate is not None else Path(spec.candidate)
    try:
        if hash_path(candidate_path) != current_identity.get("candidate_hash"):
            issues.append("candidate_artifact_hash_mismatch")
    except Exception as exc:
        issues.append(
            f"candidate_artifact_integrity_error:{type(exc).__name__}:{str(exc)[:160]}"
        )
    if portable:
        evidence_path, evidence_issues, evidence_retained = _validate_portable_file_manifest(
            record.get("evidence"), label="evidence"
        )
    else:
        evidence_path, evidence_issues = _validate_certificate_file_manifest(
            record.get("evidence"), label="evidence"
        )
        evidence_retained = evidence_path is not None and not evidence_issues
    issues.extend(evidence_issues)
    if evidence_path is not None and not evidence_issues:
        issues.extend(_validate_retained_evidence_artifacts(evidence_path))
    try:
        issues.extend(_deterministic_receipt_issues(
            record.get("deterministic_receipt"),
            spec,
            evidence_manifest=evidence_record,
            archive_receipt=(
                record.get("evidence_archive")
                if isinstance(record.get("evidence_archive"), dict)
                else {}
            ),
        ))
    except Exception as exc:
        issues.append(
            f"certificate_deterministic_receipt_invalid:{type(exc).__name__}:{str(exc)[:160]}"
        )
    if require_published:
        published = published_bot_identity(candidate_path)
        if not published.get("published"):
            issues.append("certificate_candidate_not_published")
        metadata = published.get("tag_metadata") or {}
        if metadata.get("official-certificate") != digest:
            issues.append("completion_tag_certificate_digest_mismatch")
        if metadata.get("official-candidate-hash") != current_identity.get("candidate_hash"):
            issues.append("completion_tag_candidate_hash_mismatch")
        if metadata.get("official-policy") != spec.policy_id:
            issues.append("completion_tag_policy_mismatch")
        issues.extend(
            _validate_published_attestation_at_tag(
                candidate_path,
                published,
                digest,
            )
        )
    if spec.mode == "full" and not _skip_ledger_check:
        try:
            from official_verdict_ledger import latest_authoritative_verdict

            ledger = latest_authoritative_verdict(
                str(current_identity.get("candidate_hash") or "")
            )
            if not ledger.get("valid"):
                issues.extend(ledger.get("issues") or ["official_verdict_ledger_invalid"])
            else:
                ledger_entry = ledger.get("entry")
                if not isinstance(ledger_entry, dict):
                    issues.append("official_verdict_ledger_certificate_entry_missing")
                elif ledger_entry.get("outcome") != STATUS_CERTIFIED:
                    issues.append("official_verdict_ledger_latest_outcome_not_certified")
                elif ledger_entry.get("certificate_digest") != digest:
                    issues.append("official_verdict_ledger_certificate_digest_mismatch")
        except Exception as exc:
            issues.append(
                f"official_verdict_ledger_validation_error:{type(exc).__name__}:{str(exc)[:160]}"
            )
    return {
        "valid": not issues,
        "issues": list(dict.fromkeys(issues)),
        "certificate_digest": digest,
        "spec": spec_record(spec),
        "identity": current_identity,
        "published_attestation": portable or require_published,
        "evidence_retained": evidence_retained,
        "standalone_evidence_retained": evidence_retained,
        "llm_analysis_retained": False,
        "evidence_archive_retained": bool(retained_archive_validation.get("valid")),
        "archive_evidence_available": bool(retained_archive_validation.get("valid")),
        "raw_evidence_available": bool(
            evidence_retained or retained_archive_validation.get("valid")
        ),
        "signature_valid": bool(signature_validation.get("valid")),
    }


def publish_certificate_attestation(
    status: dict[str, Any],
    candidate: str | Path,
    *,
    config: OfficialPlatformConfig | None = None,
) -> dict[str, Any]:
    """Write the compact certificate receipt that must ship with the bot commit."""
    identity = (
        status.get("certification_identity")
        if isinstance(status.get("certification_identity"), dict)
        else {}
    )
    if status.get("test_only") is True or identity.get("test_only") is not False:
        raise RuntimeError("cannot publish test-only official certificate")
    validation = certificate_validation(status, candidate=candidate, config=config)
    if not validation.get("valid"):
        raise RuntimeError(
            "cannot publish invalid official certificate: "
            + ", ".join(validation.get("issues") or [])
        )
    source = Path(str(status.get("certificate_path") or ""))
    record, _attestation, issues = _load_certificate_container(source)
    if issues or not isinstance(record, dict):
        raise RuntimeError(
            "cannot read official certificate for publication: "
            + ", ".join(issues or ["missing_record"])
        )
    signature_path = Path(str(status.get("certificate_signature_path") or source.with_suffix(".sig")))
    if signature_path.is_symlink() or not signature_path.is_file():
        raise RuntimeError("cannot publish official certificate without detached signature")
    signature = signature_path.read_text(encoding="utf-8")
    destination = published_certificate_path(candidate)
    portable_record = json.loads(json.dumps(record, ensure_ascii=False))
    try:
        relative_destination = destination.relative_to(ROOT).as_posix()
    except ValueError:
        relative_destination = str(destination)
    payload = {
        "schema_version": PUBLISHED_ATTESTATION_SCHEMA_VERSION,
        "kind": "official-platform-compliance-attestation",
        "bot": _safe_label(candidate),
        "published_at": now_iso(),
        "certificate_digest": record.get("certificate_digest"),
        "signature": signature,
        "signature_sha256": hashlib.sha256(signature.encode("utf-8")).hexdigest(),
        "issuer": record.get("issuer"),
        "raw_evidence_retention": "content-addressed-local-archive",
        "certificate": portable_record,
    }
    payload["attestation_digest"] = _attestation_payload_digest(payload)
    _write_json(destination, payload)
    return {
        "path": str(destination),
        "relative_path": relative_destination,
        "attestation_digest": payload["attestation_digest"],
        "certificate_digest": record.get("certificate_digest"),
    }


def official_full_certified(
    status: dict[str, Any],
    candidate: str | Path | None = None,
    *,
    config: OfficialPlatformConfig | None = None,
    require_published: bool = False,
) -> bool:
    status_identity = (
        status.get("certification_identity")
        if isinstance(status.get("certification_identity"), dict)
        else {}
    )
    if (
        status.get("test_only") is True
        or status_identity.get("test_only") is not False
        or status_identity.get("authority_scope") != "production"
        or status_identity.get("runner_provenance") != PRODUCTION_RUNNER_PROVENANCE
    ):
        return False
    verdict = official_compliance_verdict(status)
    if not (
        status.get("status") == STATUS_CERTIFIED
        and status.get("mode") == "full"
        and status.get("policy_id") == FULL_POLICY_ID
        and bool(verdict.get("ok"))
        and not bool(verdict.get("inconclusive"))
        and not bool(verdict.get("blocking"))
    ):
        return False
    validation = certificate_validation(
        status,
        candidate=candidate,
        config=config,
        require_published=require_published,
    )
    if not validation.get("valid"):
        return False
    identity = validation.get("identity") if isinstance(validation.get("identity"), dict) else {}
    from official_verdict_ledger import latest_authoritative_verdict

    ledger = latest_authoritative_verdict(str(identity.get("candidate_hash") or ""))
    entry = ledger.get("entry") if ledger.get("valid") else None
    return bool(
        isinstance(entry, dict)
        and entry.get("outcome") == STATUS_CERTIFIED
        and entry.get("certificate_digest") == status.get("certificate_digest")
    )


def authoritative_verdict_status_issues(
    status: Any,
    *,
    _validated_ledger_entries: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Validate a status before the certifier signs it into the verdict ledger."""
    if not isinstance(status, dict):
        return [f"official_verdict_status_invalid_type:{type(status).__name__}"]
    issues: list[str] = []
    outcome = str(status.get("status") or "")
    if outcome not in {STATUS_CERTIFIED, STATUS_FAILED, STATUS_INCONCLUSIVE}:
        issues.append("official_verdict_status_outcome_not_formal")
    if status.get("mode") != "full" or status.get("policy_id") != FULL_POLICY_ID:
        issues.append("official_verdict_status_policy_not_full")
    identity = (
        status.get("certification_identity")
        if isinstance(status.get("certification_identity"), dict)
        else {}
    )
    if status.get("test_only") is True or identity.get("test_only") is not False:
        issues.append("official_verdict_status_test_only")
    if identity.get("authority_scope") != "production":
        issues.append("official_verdict_status_authority_scope_invalid")
    if identity.get("runner_provenance") != PRODUCTION_RUNNER_PROVENANCE:
        issues.append("official_verdict_status_runner_provenance_invalid")
    try:
        spec = _spec_from_mapping(identity.get("spec") or {})
    except Exception as exc:
        return list(dict.fromkeys([
            *issues,
            f"official_verdict_status_spec_invalid:{type(exc).__name__}:{str(exc)[:160]}",
        ]))
    if spec.mode != "full" or spec.policy_id != FULL_POLICY_ID:
        issues.append("official_verdict_status_spec_not_full")
    issues.extend(_identity_integrity_issues(identity, spec))
    candidate_hash = str(identity.get("candidate_hash") or "")
    try:
        if len(candidate_hash) != 64 or hash_path(spec.candidate) != candidate_hash:
            issues.append("official_verdict_status_candidate_identity_invalid")
    except Exception as exc:
        issues.append(
            f"official_verdict_status_candidate_identity_error:{type(exc).__name__}:{str(exc)[:120]}"
        )
    if status.get("bot") != _safe_label(spec.candidate):
        issues.append("official_verdict_status_candidate_label_mismatch")
    envelope = (
        status.get("official_job_envelope")
        if isinstance(status.get("official_job_envelope"), dict)
        else None
    )
    try:
        from official_job_envelope import job_envelope_issues

        issues.extend(job_envelope_issues(
            envelope,
            expected_candidate_hash=candidate_hash,
            expected_opponent_hash=str(identity.get("opponent_hash") or ""),
        ))
    except Exception as exc:
        issues.append(
            f"official_verdict_status_job_envelope_error:{type(exc).__name__}:{str(exc)[:120]}"
        )
    result = status.get("result") if isinstance(status.get("result"), dict) else {}
    issues.extend(_job_envelope_report_issues(result, envelope))
    started = status.get("request_started_ns")
    completed = status.get("request_completed_ns")
    if (
        not isinstance(started, int)
        or isinstance(started, bool)
        or not isinstance(completed, int)
        or isinstance(completed, bool)
        or started <= 0
        or completed < started
    ):
        issues.append("official_verdict_status_request_interval_invalid")
    if outcome == STATUS_CERTIFIED:
        validation = certificate_validation(
            status,
            candidate=spec.candidate,
            _skip_ledger_check=True,
            _validated_ledger_entries=_validated_ledger_entries,
        )
        issues.extend(validation.get("issues") or [])
    else:
        issues.extend(_deterministic_status_receipt_issues(
            status,
            candidate=spec.candidate,
        ))
        receipt = status.get("official_deterministic_status_receipt")
        verdict = receipt.get("verdict") if isinstance(receipt, dict) else {}
        verdict = verdict if isinstance(verdict, dict) else {}
        if outcome == STATUS_FAILED and not (
            verdict.get("blocking") is True
            and verdict.get("inconclusive") is False
        ):
            issues.append("official_verdict_status_failure_not_deterministically_blocking")
        if outcome == STATUS_INCONCLUSIVE and verdict.get("inconclusive") is not True:
            issues.append("official_verdict_status_inconclusive_not_deterministic")
    return list(dict.fromkeys(str(issue) for issue in issues if str(issue)))


def _official_verdict_ledger_issues() -> list[str]:
    try:
        from official_verdict_ledger import ledger_integrity

        validation = ledger_integrity()
    except Exception as exc:
        return [
            f"official_verdict_ledger_validation_error:{type(exc).__name__}:{str(exc)[:160]}"
        ]
    if validation.get("valid"):
        return []
    return list(validation.get("issues") or ["official_verdict_ledger_invalid"])


def parent_eligible(candidate: str | Path) -> bool:
    return bool(strict_role_eligibility(candidate, "parent_source").get("eligible"))


def active_pool_eligible(candidate: str | Path) -> bool:
    parent = strict_role_eligibility(candidate, "parent_source")
    rating = strict_role_eligibility(candidate, "rating_pool")
    return bool(parent.get("eligible") and rating.get("eligible"))


def _digest_bound_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "receipt_digest": canonical_digest(payload)}


def _official_certificate_opponent_receipt(
    candidate: str | Path,
    status: dict[str, Any],
) -> dict[str, Any]:
    identity = published_bot_identity(candidate)
    return _digest_bound_receipt({
        "schema_version": OPPONENT_ELIGIBILITY_RECEIPT_SCHEMA_VERSION,
        "kind": "official_full_certificate",
        "role": "official_opponent",
        "bot": str(identity.get("label") or Path(candidate).name),
        "artifact_hash": str(identity.get("artifact_hash") or ""),
        "policy_id": str(status.get("policy_id") or ""),
        "certificate_digest": str(status.get("certificate_digest") or ""),
    })


def stable_official_opponent_selection(
    selection: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the immutable authorization receipt used by jobs and certificates."""
    if not isinstance(selection, dict):
        return None
    opponent = selection.get("opponent") if isinstance(selection.get("opponent"), dict) else {}
    stable = {
        "selected": bool(selection.get("selected")),
        "candidate": str(selection.get("candidate") or ""),
        "opponent": {
            key: opponent.get(key)
            for key in (
                "bot",
                "path",
                "artifact_hash",
                "tag",
                "tag_object",
                "eligible",
                "reason",
                "eligibility_receipt",
            )
        },
    }
    # Keep ordinary v5 selection records byte-compatible.  Bootstrap fields
    # exist only on the explicit one-time path and are part of its job/cert
    # identity, not a fallback for normal opponent selection.
    bootstrap_control_id = selection.get("bootstrap_control_id")
    if bootstrap_control_id is not None:
        stable["eligible"] = bool(selection.get("eligible"))
        stable["reason"] = selection.get("reason")
        stable["kind"] = selection.get("kind")
        stable["bootstrap_control_id"] = str(bootstrap_control_id or "")
        stable["bootstrap_control_receipt"] = selection.get(
            "bootstrap_control_receipt"
        )
        stable["candidate_binding"] = selection.get("candidate_binding")
        stable["operator_bootstrap_authorization"] = selection.get(
            "operator_bootstrap_authorization"
        )
        # These negative-authority flags are part of the control receipt, not
        # optional diagnostics.  Dropping them while freezing a durable job
        # would make the later exact selector validator compare ``None`` with
        # ``False`` and, more importantly, would lose the zero-strength role
        # boundary from the certificate identity.
        for key in (
            "authority",
            "normal_official_opponent",
            "strength_admitted",
            "rating_eligible",
        ):
            stable["opponent"][key] = opponent.get(key)
    return stable


def official_opponent_eligibility(
    candidate: str | Path,
    *,
    allow_bootstrap_grandfather: bool = False,
    target_version: int | None = None,
    certified_alternatives: int | None = None,
) -> dict[str, Any]:
    """Return formal official-EXE opponent eligibility.

    A published signed full certificate is the sole production authorization.
    ``allow_bootstrap_grandfather`` is retained for API compatibility only;
    it never authorizes a content-bound migration grant in this path.
    """
    version = parse_bot_version(Path(candidate).name)
    lifecycle = (
        epoch_lifecycle_eligibility(version)
        if version is not None
        else {"eligible": False, "reason": "invalid_national_bot_label"}
    )
    if not lifecycle.get("eligible"):
        return {
            "eligible": False,
            "reason": lifecycle.get("reason") or "national_epoch_ineligible",
            "lifecycle": lifecycle,
        }
    status = read_status(candidate)
    verdict = official_compliance_verdict(status)
    if bool(verdict.get("blocking")):
        return {
            "eligible": False,
            "reason": "blocking_official_failure",
            "status": status.get("status"),
            "mode": status.get("mode"),
            "verdict": verdict,
        }
    authorization = strict_role_eligibility(candidate, "official_opponent")
    if not authorization.get("eligible"):
        issues = list(authorization.get("issues") or [])
        certificate_missing = any(
            issue in {
                "signed_full_official_certificate_required",
                "official_certificate_digest_invalid",
            }
            for issue in issues
        )
        return {
            "eligible": False,
            "reason": (
                "official_full_certificate_required"
                if certificate_missing
                else authorization.get("reason") or "strict_national_bot_spec_required"
            ),
            "authorization": authorization,
            "bootstrap_requested_but_disabled": bool(allow_bootstrap_grandfather),
            "grandfathered": False,
        }
    if official_full_certified(status, candidate, require_published=True):
        reason = "official_certified"
        priority = 0
        eligibility_receipt = _official_certificate_opponent_receipt(candidate, status)
    else:
        return {
            "eligible": False,
            "reason": "official_full_certificate_required",
            "status": status.get("status"),
            "mode": status.get("mode"),
            "verdict": verdict,
            "bootstrap_requested_but_disabled": bool(allow_bootstrap_grandfather),
            "grandfathered": False,
        }
    return {
        "eligible": True,
        "reason": reason,
        "priority": priority,
        "status": status.get("status"),
        "mode": status.get("mode"),
        "verdict": verdict,
        "grandfathered": False,
        "eligibility_receipt": eligibility_receipt,
    }


def _bot_path_from_token(token: str | Path) -> Path:
    raw = Path(token).expanduser()
    if raw.is_absolute() or len(raw.parts) > 1:
        return raw.resolve()
    version = parse_bot_version(str(token))
    if version is not None:
        return (ROOT / "bots" / bot_name(version)).resolve()
    return (ROOT / "bots" / str(token)).resolve()


def _same_bot_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except Exception:
        return str(a) == str(b)


def select_official_opponent(
    candidate: str | Path,
    active_bots: list[str] | tuple[str, ...] | None = None,
    *,
    preferred: str | Path | None = None,
    allow_bootstrap_grandfather: bool = False,
) -> dict[str, Any]:
    candidate_path = _bot_path_from_token(candidate)
    try:
        candidate_artifact_hash = hash_path(candidate_path)
    except Exception:
        candidate_artifact_hash = ""
    target_version = parse_bot_version(candidate_path.name)
    try:
        from evolution_infra import get_active_bots, load_reaped_bot_versions

        reaped_versions = load_reaped_bot_versions()
        if active_bots is None:
            active_bots = get_active_bots()
        lifecycle_error = ""
    except Exception as exc:
        reaped_versions = None
        if active_bots is None:
            active_bots = []
        lifecycle_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    raw_tokens: list[str | Path] = []
    if preferred:
        raw_tokens.append(preferred)
    raw_tokens.extend(active_bots or [])

    unique_paths: list[Path] = []
    unique_seen: set[str] = set()
    for token in raw_tokens:
        path = _bot_path_from_token(token)
        if str(path) not in unique_seen and not _same_bot_path(path, candidate_path):
            unique_seen.add(str(path))
            unique_paths.append(path)
    certified_alternative_artifacts: set[str] = set()
    for path in unique_paths:
        try:
            alternative_status = read_status(path)
            if official_full_certified(
                alternative_status,
                path,
                require_published=True,
            ):
                artifact_hash = str(
                    (alternative_status.get("certification_identity") or {}).get("candidate_hash")
                    or ""
                )
                if artifact_hash:
                    if artifact_hash == candidate_artifact_hash:
                        continue
                    certified_alternative_artifacts.add(artifact_hash)
        except Exception:
            continue
    certified_alternatives = len(certified_alternative_artifacts)

    considered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in raw_tokens:
        path = _bot_path_from_token(token)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        name = path.name
        if _same_bot_path(path, candidate_path):
            considered.append({"bot": name, "path": str(path), "eligible": False, "reason": "candidate_self"})
            continue
        if not path.exists() or not (path / "national_bot.py").exists():
            considered.append({"bot": name, "path": str(path), "eligible": False, "reason": "missing_native_entry"})
            continue
        if not (path / ".completed").exists():
            considered.append({"bot": name, "path": str(path), "eligible": False, "reason": "missing_completed_sentinel"})
            continue
        if reaped_versions is None:
            considered.append({
                "bot": name,
                "path": str(path),
                "eligible": False,
                "reason": "lifecycle_ledger_unavailable",
                "error": lifecycle_error,
            })
            continue
        identity = published_bot_identity(path)
        if not identity.get("published"):
            considered.append({
                "bot": name,
                "path": str(path),
                "eligible": False,
                "reason": "not_published_artifact",
                "identity_issues": identity.get("issues") or [],
            })
            continue
        if (
            candidate_artifact_hash
            and identity.get("artifact_hash") == candidate_artifact_hash
        ):
            considered.append({
                "bot": name,
                "path": str(path),
                "artifact_hash": identity.get("artifact_hash"),
                "eligible": False,
                "reason": "candidate_artifact_clone",
            })
            continue
        version = parse_bot_version(name)
        if version is None or version in reaped_versions:
            considered.append({
                "bot": name,
                "path": str(path),
                "eligible": False,
                "reason": "reaped_or_invalid_version",
            })
            continue
        try:
            from national_native import check_native_contract
            from national_runtime_authority import (
                current_system_native_runtime_errors,
            )

            native_errors = [
                *current_system_native_runtime_errors(path),
                *check_native_contract(path),
            ]
        except Exception as exc:
            native_errors = [f"native_contract_check_error:{type(exc).__name__}:{str(exc)[:200]}"]
        if native_errors:
            considered.append({
                "bot": name,
                "path": str(path),
                "eligible": False,
                "reason": "native_contract_failed",
                "native_errors": native_errors[:5],
            })
            continue
        eligibility = official_opponent_eligibility(
            path,
            allow_bootstrap_grandfather=allow_bootstrap_grandfather,
            target_version=target_version,
            certified_alternatives=certified_alternatives,
        )
        if (
            eligibility.get("eligible")
            and eligibility.get("reason") != "official_certified"
        ):
            # Keep diagnostics about a stale/injected authorization, but never
            # expose it as an eligible formal opponent to downstream callers.
            eligibility = {
                **eligibility,
                "eligible": False,
                "reason": "official_full_certificate_required",
                "rejected_authorization_reason": eligibility.get("reason"),
                "bootstrap_requested_but_disabled": bool(
                    allow_bootstrap_grandfather
                ),
                "grandfathered": False,
            }
        item = {
            "bot": name,
            "path": str(path),
            "artifact_hash": identity.get("artifact_hash"),
            "tag": identity.get("tag"),
            "tag_object": identity.get("tag_object"),
            **eligibility,
        }
        considered.append(item)

    # Defense in depth: even a stale/injected helper result must not make a
    # content-bound grandfather receipt selectable for a formal EXE job.
    eligible = [
        item
        for item in considered
        if item.get("eligible") and item.get("reason") == "official_certified"
    ]
    if not eligible:
        return {
            "selected": False,
            "reason": "no_official_eligible_opponent",
            "candidate": str(candidate_path),
            "considered": considered,
            "readiness": {
                "certified_alternatives": certified_alternatives,
                "minimum_certified_alternatives": 2,
            },
        }
    selected = sorted(
        eligible,
        key=lambda item: (int(item.get("priority", 99)), -(parse_bot_version(item.get("bot")) or 0)),
    )[0]
    return {
        "selected": True,
        "candidate": str(candidate_path),
        "opponent": selected,
        "considered": considered,
        "readiness": {
            "certified_alternatives": certified_alternatives,
            "minimum_certified_alternatives": 2,
        },
    }


def resolve_managed_certification_spec(
    spec: CertificationSpec,
    *,
    exact_opponent_only: bool = False,
) -> tuple[CertificationSpec | None, dict[str, Any] | None]:
    """Revalidate/reselect the opponent immediately before formal EXE work.

    Durable jobs have already frozen an opponent path and its authorization
    receipt into their identity.  Their worker must revalidate that exact
    artifact without depending on an unrelated scan of the mutable active
    pool; all identity and receipt fields are compared again by the caller.
    """
    if spec.opponent_rounds <= 0:
        return spec, None
    if spec.bootstrap_control_id is not None:
        # Bootstrap is opt-in and never falls back to an active or archived bot.
        from official_bootstrap import (
            authorize_operator_bootstrap_selection,
            select_first_strict_control,
        )

        selection = select_first_strict_control(
            spec.bootstrap_control_id,
            spec.candidate,
        )
        if selection.get("selected"):
            authorization = authorize_operator_bootstrap_selection(
                selection,
                spec.bootstrap_control_id,
                spec.candidate,
            )
            if authorization.get("valid") is not True:
                return None, authorization
            selection = authorization["selection"]
    else:
        selection = select_official_opponent(
            spec.candidate,
            active_bots=(spec.opponent,) if exact_opponent_only else None,
            preferred=spec.opponent,
            allow_bootstrap_grandfather=False,
        )
    if not selection.get("selected"):
        return None, selection
    selected_path = str(Path(selection["opponent"]["path"]).resolve())
    if spec.opponent == selected_path:
        return spec, selection
    resolved = build_spec(
        spec.mode,
        spec.candidate,
        opponent=selected_path,
        self_play_rounds=spec.self_play_rounds,
        opponent_rounds=spec.opponent_rounds,
        target_hands=spec.target_hands,
        round_timeout_sec=spec.round_timeout_sec,
        no_progress_timeout_sec=spec.no_progress_timeout_sec,
        bootstrap_control_id=spec.bootstrap_control_id,
    )
    return resolved, selection


def official_lock_busy(config: OfficialPlatformConfig | None = None) -> bool:
    cfg = config or OfficialPlatformConfig()
    return official_platform_busy(cfg.lock_path)


def _evidence_path_for_result(spec: CertificationSpec, summary: dict[str, Any], cache_key_value: str) -> Path:
    suite_dir = summary.get("suite_dir") if isinstance(summary, dict) else None
    if suite_dir:
        suite_path = Path(str(suite_dir))
        if suite_path.exists():
            return suite_path / "official_evidence.json"
    safe_key = cache_key_value[:12] if cache_key_value else "uncached"
    return certification_root() / "evidence" / spec.mode / f"{_safe_label(spec.candidate)}-{safe_key}.json"


def _official_llm_analysis_enabled() -> bool:
    return os.environ.get("POK_OFFICIAL_LLM_ANALYSIS", "1").strip().lower() in {"1", "true", "yes", "on"}


def _short_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _write_certificate_record(
    spec: CertificationSpec,
    identity: dict[str, Any],
    evidence_extra: dict[str, Any],
    cache_key_value: str,
    opponent_selection: dict[str, Any] | None = None,
    job_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from official_certificate_signing import (
        sign_certificate,
        signing_identity,
        verify_certificate_signature,
    )

    stable_selection = stable_official_opponent_selection(opponent_selection)
    selection_issues = _opponent_selection_issues(stable_selection, spec, identity)
    if selection_issues:
        raise RuntimeError(
            "official opponent authorization receipt is invalid: "
            + ", ".join(selection_issues)
        )
    from official_job_envelope import job_envelope_issues

    envelope_issues = job_envelope_issues(
        job_envelope,
        expected_candidate_hash=str(identity.get("candidate_hash") or ""),
        expected_opponent_hash=str(identity.get("opponent_hash") or ""),
    )
    if envelope_issues:
        raise RuntimeError(
            "official durable job envelope is invalid: "
            + ", ".join(envelope_issues)
        )
    evidence_path = Path(str(evidence_extra.get("official_evidence_path") or ""))
    payload = {
        "schema_version": CERTIFICATE_SCHEMA_VERSION,
        "kind": "official-exe-compliance-certificate",
        "candidate_label": _safe_label(spec.candidate),
        "issuer": signing_identity(),
        "issued_at": now_iso(),
        "policy_id": spec.policy_id,
        "mode": spec.mode,
        "spec": spec_record(spec),
        "identity": identity,
        "cache_key": cache_key_value,
        "opponent_selection": stable_selection,
        "job_envelope": job_envelope,
        "evidence_archive": evidence_extra.get("official_evidence_archive"),
        "evidence": {
            **_certificate_file_manifest(evidence_path, label="official evidence"),
            "summary": evidence_extra.get("official_evidence_summary") or {},
        },
        "deterministic_receipt": evidence_extra.get("official_deterministic_receipt"),
        "strength_evaluation": "not_applicable",
    }
    digest = canonical_digest(payload)
    path = (
        certificate_dir()
        / str(identity.get("candidate_hash") or "missing")
        / f"{digest}.json"
    )
    record = {**payload, "certificate_digest": digest}
    _write_json(path, record)
    signature = sign_certificate(record)
    signature_path = path.with_suffix(".sig")
    signature_path.write_text(signature, encoding="utf-8")
    signature_validation = verify_certificate_signature(record, signature)
    if not signature_validation.get("valid"):
        raise RuntimeError(
            "official certificate signature self-check failed: "
            + ", ".join(signature_validation.get("issues") or [])
        )
    return {
        **record,
        "certificate_path": str(path),
        "certificate_signature_path": str(signature_path),
        "certificate_signature_sha256": _file_sha256(signature_path),
    }


def official_feedback_summary(*, limit: int = 8, max_chars: int = 6000) -> str:
    """Return bounded official-EXE compliance feedback for planning prompts.

    This is compliance-only context.  Win/loss and score outcomes from the
    official EXE are intentionally excluded so the Master cannot treat the
    platform as a strength evaluator.
    """
    rows: list[dict[str, Any]] = []
    try:
        files = sorted(status_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        files = []
    for path in files:
        payload = _read_json(path) or {}
        if not payload:
            continue
        verdict = official_compliance_verdict(payload)
        llm_summary = payload.get("official_llm_analysis_summary") or {}
        repair_guidance = payload.get("official_llm_repair_guidance") or llm_summary.get("repair_guidance")
        prompt_feedback = payload.get("official_llm_prompt_feedback") or llm_summary.get("prompt_feedback")
        issues = payload.get("issues") or []
        has_signal = (
            verdict.get("blocking")
            or verdict.get("inconclusive")
            or repair_guidance
            or prompt_feedback
            or payload.get("status") in {STATUS_FAILED, STATUS_INCONCLUSIVE}
        )
        if not has_signal:
            continue
        rows.append({
            "bot": payload.get("bot") or path.stem,
            "status": payload.get("status"),
            "mode": payload.get("mode"),
            "classification": verdict.get("classification"),
            "blocking": bool(verdict.get("blocking")),
            "inconclusive": bool(verdict.get("inconclusive")),
            "issues": issues[:5],
            "evidence_path": payload.get("official_evidence_path"),
            "repair_guidance": _short_text(repair_guidance, 900),
            "prompt_feedback": _short_text(prompt_feedback, 900),
        })
        if len(rows) >= limit:
            break
    if not rows:
        return "No official EXE compliance feedback recorded yet."

    lines = [
        "Official EXE feedback is compliance-only; do not use EXE wins/losses as strength evidence.",
    ]
    for row in rows:
        lines.append(
            f"- {row['bot']}: status={row['status']} mode={row['mode']} "
            f"classification={row['classification']} blocking={row['blocking']} "
            f"inconclusive={row['inconclusive']}"
        )
        if row["issues"]:
            lines.append("  issues: " + "; ".join(str(item)[:180] for item in row["issues"]))
        if row["repair_guidance"]:
            lines.append("  repair_guidance: " + row["repair_guidance"])
        if row["prompt_feedback"]:
            lines.append("  prompt_feedback: " + row["prompt_feedback"])
        if row["evidence_path"]:
            lines.append(f"  evidence: {row['evidence_path']}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text


def _status_for_result(
    spec: CertificationSpec,
    result: dict[str, Any],
    *,
    cache_hit: bool,
    cache_key_value: str,
    identity: dict[str, Any],
    request_started_ns: int,
    identity_issues: list[str] | None = None,
    opponent_selection: dict[str, Any] | None = None,
    job_envelope: dict[str, Any] | None = None,
    test_only: bool = False,
) -> dict[str, Any]:
    validation_issues = report_validation_issues(result, spec)
    valid = not validation_issues
    raw_result_issues = result.get("issues") or []
    if not isinstance(raw_result_issues, list):
        raw_result_issues = [raw_result_issues]
    issues = list(dict.fromkeys([
        str(issue)
        for issue in raw_result_issues + validation_issues + list(identity_issues or [])
    ]))
    if valid:
        if spec.mode == "full":
            status = STATUS_CERTIFIED
        elif spec.mode == "compliance":
            status = STATUS_COMPLIANCE_PASS
        else:
            status = STATUS_SMOKE_PASS
    elif _issues_have_protocol_violation(issues):
        status = STATUS_FAILED
    else:
        status = STATUS_INCONCLUSIVE
    if identity_issues and status != STATUS_FAILED:
        status = STATUS_INCONCLUSIVE
    report = result.get("report", {}) if isinstance(result, dict) else {}
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    evidence_extra: dict[str, Any] = {}
    evidence: dict[str, Any] | None = None
    try:
        evidence_path = _evidence_path_for_result(spec, summary, cache_key_value)
        evidence_result = dict(result)
        evidence_result["issues"] = issues
        evidence = build_official_evidence_bundle(evidence_result, output_path=evidence_path)
        deterministic = evidence.get("deterministic", {})
        evidence_issues = [
            str(issue)
            for issue in (deterministic.get("issues") or [])
            if str(issue)
        ]
        if evidence_issues:
            issues = list(dict.fromkeys([*issues, *evidence_issues]))
        if deterministic.get("blocking"):
            status = STATUS_FAILED
        elif deterministic.get("inconclusive"):
            status = STATUS_INCONCLUSIVE
        evidence_extra = {
            "official_evidence_path": str(evidence_path),
            "official_evidence_summary": {
                "schema_version": evidence.get("schema_version"),
                "classification": deterministic.get("classification"),
                "blocking": deterministic.get("blocking"),
                "inconclusive": deterministic.get("inconclusive"),
                "violation": deterministic.get("violation"),
                "issue_count": len(deterministic.get("issues") or []),
                "rounds_requested": deterministic.get("rounds_requested"),
                "rounds_run": deterministic.get("rounds_run"),
                "target_hands": deterministic.get("target_hands"),
                "strength_evaluation": "not_applicable",
            },
        }
        if spec.mode == "full":
            archive = build_evidence_archive(summary.get("suite_dir"))
            archive_validation = validate_evidence_archive(
                archive,
                expected_evidence_sha256=_file_sha256(evidence_path),
            )
            if not archive_validation.get("valid"):
                issues = list(dict.fromkeys([
                    *issues,
                    *(archive_validation.get("issues") or ["official_evidence_archive_invalid"]),
                ]))
                status = STATUS_INCONCLUSIVE
            else:
                evidence_extra["official_evidence_archive"] = archive
                if status == STATUS_CERTIFIED:
                    deterministic_receipt = _build_deterministic_receipt(
                        spec,
                        evidence,
                        evidence_path,
                        archive,
                    )
                    receipt_issues = _deterministic_receipt_issues(
                        deterministic_receipt,
                        spec,
                        evidence_manifest={"sha256": _file_sha256(evidence_path)},
                        archive_receipt=archive,
                    )
                    if receipt_issues:
                        issues = list(dict.fromkeys([*issues, *receipt_issues]))
                        status = STATUS_INCONCLUSIVE
                    else:
                        evidence_extra["official_deterministic_receipt"] = deterministic_receipt
        evidence_extra["official_deterministic_status_receipt"] = (
            _build_deterministic_status_receipt(
                spec,
                identity,
                evidence_path,
                deterministic,
                cache_key_value,
                evidence_extra.get("official_evidence_archive"),
            )
        )
    except Exception as exc:
        issue = f"official_evidence_error: {type(exc).__name__}: {str(exc)[:300]}"
        issues = list(dict.fromkeys([*issues, issue]))
        status = STATUS_INCONCLUSIVE
        # Preserve a successfully-written evidence bundle when a later archive
        # or certificate step fails. This keeps the run diagnosable and avoids
        # dereferencing a removed evidence path in advisory analysis.
        evidence_extra = {
            **evidence_extra,
            "official_evidence_error": issue,
        }
    certificate_extra: dict[str, Any] = {}
    if status == STATUS_CERTIFIED and spec.mode == "full":
        try:
            record = _write_certificate_record(
                spec,
                identity,
                evidence_extra,
                cache_key_value,
                opponent_selection,
                job_envelope,
            )
            certificate_extra = {
                "certificate_schema_version": record.get("schema_version"),
                "certificate_digest": record.get("certificate_digest"),
                "certificate_path": record.get("certificate_path"),
                "certificate_signature_path": record.get("certificate_signature_path"),
                "certificate_signature_sha256": record.get("certificate_signature_sha256"),
            }
        except Exception as exc:
            issues = list(dict.fromkeys([
                *issues,
                f"official_certificate_artifact_error:{type(exc).__name__}:{str(exc)[:240]}",
            ]))
            status = STATUS_INCONCLUSIVE
    if evidence is not None and evidence_extra.get("official_evidence_path"):
        evidence_sha256 = _file_sha256(Path(evidence_extra["official_evidence_path"]))
        analysis_identity = canonical_digest({
            "evidence_sha256": evidence_sha256,
            "analysis_sha256": (
                _file_sha256(LLM_ANALYSIS_PATH) if LLM_ANALYSIS_PATH.exists() else "missing"
            ),
            "prompt_sha256": (
                _file_sha256(LLM_ANALYSIS_PROMPT_PATH)
                if LLM_ANALYSIS_PROMPT_PATH.exists()
                else "missing"
            ),
        })
        analysis_path = (
            certification_root()
            / "analysis"
            / _safe_label(spec.candidate)
            / f"{analysis_identity}.json"
        )
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_issue = ""
        try:
            if _official_llm_analysis_enabled():
                from official_llm_analysis import (
                    advisory_analysis_contract_issues,
                    run_official_llm_analysis_sync,
                )

                analysis = run_official_llm_analysis_sync(evidence, output_path=analysis_path)
                analysis_issues = advisory_analysis_contract_issues(analysis)
                if analysis_issues:
                    raise ValueError(";".join(analysis_issues))
                analysis.setdefault("analysis_source", "llm")
            else:
                from official_llm_analysis import safe_default_analysis

                analysis = safe_default_analysis(evidence, reason="llm_disabled")
                analysis["analysis_path"] = str(analysis_path)
                _write_json(analysis_path, analysis)
        except Exception as exc:
            analysis_issue = f"{type(exc).__name__}: {str(exc)[:300]}"
            try:
                from official_llm_analysis import safe_default_analysis

                analysis = safe_default_analysis(
                    evidence,
                    reason=f"llm_unavailable:{type(exc).__name__}",
                )
            except Exception:
                analysis = {
                    "analysis_source": "unavailable",
                    "repair_guidance": "",
                    "prompt_feedback": "",
                    "confidence": 0.0,
                    "strength_evaluation": "not_applicable",
                }
            analysis["analysis_path"] = str(analysis_path)
            _write_json(analysis_path, analysis)
        evidence_extra["official_llm_analysis_path"] = str(analysis_path)
        evidence_extra["official_llm_analysis_issue"] = analysis_issue
        evidence_extra["official_llm_analysis_summary"] = {
            "analysis_source": analysis.get("analysis_source"),
            "analysis_status": analysis.get("analysis_status"),
            "hypothesis_class": analysis.get("hypothesis_class"),
            "authority": analysis.get("authority"),
            "confidence": analysis.get("confidence"),
            "repair_guidance": _short_text(analysis.get("repair_guidance"), 1200),
            "prompt_feedback": _short_text(analysis.get("prompt_feedback"), 1200),
            "strength_evaluation": "not_applicable",
            "authoritative": False,
        }
        evidence_extra["official_llm_repair_guidance"] = _short_text(
            analysis.get("repair_guidance"), 2000
        )
        evidence_extra["official_llm_prompt_feedback"] = _short_text(
            analysis.get("prompt_feedback"), 2000
        )
    written = write_status(
        spec.candidate,
        status,
        mode=spec.mode,
        policy_id=spec.policy_id,
        cache_hit=cache_hit,
        cache_key=cache_key_value,
        certification_identity=identity,
        test_only=bool(test_only),
        authority_scope="test-only" if test_only else "production",
        summary=summary,
        issues=issues,
        result=result,
        opponent_selection=opponent_selection,
        official_job_envelope=job_envelope,
        request_started_ns=request_started_ns,
        request_completed_ns=time.time_ns(),
        **evidence_extra,
        **certificate_extra,
    )
    if (
        spec.mode == "full"
        and not test_only
        and written.get("request_started_ns") == request_started_ns
    ):
        try:
            from official_verdict_ledger import append_verdict

            ledger_entry = append_verdict(written)
            written = {
                **written,
                "official_verdict_ledger_entry": ledger_entry,
            }
        except Exception as exc:
            ledger_issue = (
                "official_verdict_ledger_error: "
                f"{type(exc).__name__}: {str(exc)[:240]}"
            )
            written = {
                **written,
                "status": STATUS_INCONCLUSIVE,
                "status_label": STATUS_INCONCLUSIVE,
                "issues": list(dict.fromkeys([
                    *(written.get("issues") or []),
                    ledger_issue,
                ])),
                "official_verdict_ledger_error": ledger_issue,
            }
        with _status_lock(_safe_label(spec.candidate)):
            current = _read_json(_status_path(_safe_label(spec.candidate))) or {}
            if current.get("request_started_ns") == request_started_ns:
                _write_json(_status_path(_safe_label(spec.candidate)), written)
    return written


def _run_certification_impl(
    spec: CertificationSpec,
    *,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    runner: Runner,
    runner_provenance: str,
    enforce_opponent_selection: bool,
    request_started_ns: int | None = None,
    opponent_selection: dict[str, Any] | None = None,
    suite_dir: str | Path | None = None,
    job_envelope: dict[str, Any] | None = None,
    test_only: bool = False,
    _production_authority: object | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(request_started_ns, int)
        or isinstance(request_started_ns, bool)
        or request_started_ns <= 0
    ):
        request_started_ns = time.time_ns()
    validate_spec(spec)
    if test_only:
        if "PYTEST_CURRENT_TEST" not in os.environ:
            raise RuntimeError("test-only certification runner is available only under pytest")
        if runner_provenance != TEST_ONLY_RUNNER_PROVENANCE:
            raise RuntimeError("test-only certification must use test-only runner provenance")
    elif runner_provenance != PRODUCTION_RUNNER_PROVENANCE:
        raise RuntimeError("production certification requires official-exe runner provenance")
    if spec.mode == "full":
        if not test_only and (
            runner is not _PRODUCTION_CERTIFICATION_RUNNER
            or _production_authority is not _PRODUCTION_FULL_AUTHORITY
        ):
            raise RuntimeError(
                "formal full certification requires the bound production official-EXE runner"
            )
        from official_job_envelope import job_envelope_issues

        envelope_issues = job_envelope_issues(job_envelope)
        if envelope_issues:
            raise RuntimeError(
                "formal full certification requires a valid durable job envelope: "
                + ", ".join(envelope_issues)
            )
        if not test_only:
            ledger_issues = _official_verdict_ledger_issues()
            if ledger_issues:
                raise RuntimeError(
                    "official_verdict_ledger_preflight_failed: "
                    + "; ".join(ledger_issues)
                    + "; explicitly initialize genesis with "
                    "python3 scripts/official_certify.py init-ledger"
                )
    if enforce_opponent_selection:
        resolved_spec, opponent_selection = resolve_managed_certification_spec(spec)
        if resolved_spec is None:
            return {
                "bot": _safe_label(spec.candidate),
                "status": "opponent-selection-blocked",
                "status_label": "opponent-selection-blocked",
                "mode": spec.mode,
                "updated_at": now_iso(),
                "issues": ["no_official_eligible_opponent"],
                "blocking": False,
                "inconclusive": True,
                "opponent_selection": opponent_selection,
            }
        spec = resolved_spec
    cfg = config or OfficialPlatformConfig()
    cfg = _copy_config(
        cfg,
        round_timeout_sec=spec.round_timeout_sec,
        no_progress_timeout_sec=spec.no_progress_timeout_sec,
        results_dir=certification_root() / spec.mode,
    )
    identity_before = certification_identity(
        spec,
        cfg,
        runner_provenance=runner_provenance,
        test_only=test_only,
    )
    key = (
        canonical_digest({
            "certification_identity": identity_before,
            "job_envelope": job_envelope,
        })
        if spec.mode == "full"
        else str(identity_before["identity_digest"])
    )
    if not force and spec.mode != "full":
        cached = _cache_hit(
            spec,
            cfg,
            runner_provenance=runner_provenance,
            test_only=test_only,
        )
        if cached:
            return _status_for_result(
                spec,
                cached["result"],
                cache_hit=True,
                cache_key_value=key,
                identity=identity_before,
                request_started_ns=request_started_ns,
                opponent_selection=opponent_selection,
                job_envelope=job_envelope,
                test_only=test_only,
            )
    if spec.mode == "full":
        from official_certificate_signing import signing_environment_report

        signing_report = signing_environment_report()
        if not signing_report.get("ok"):
            raise RuntimeError(
                "official_certificate_signing_preflight_failed: "
                + "; ".join(signing_report.get("issues") or ["unknown signing error"])
            )

    runner_kwargs = {
        "opponent": spec.opponent,
        "self_play_rounds": spec.self_play_rounds,
        "opponent_rounds": spec.opponent_rounds,
        "target_hands": spec.target_hands,
        "config": cfg,
    }
    if suite_dir is not None:
        runner_kwargs["suite_dir"] = Path(suite_dir).expanduser().resolve()
    if job_envelope is not None:
        runner_kwargs["job_envelope"] = job_envelope
    result_obj = runner(spec.candidate, **runner_kwargs)
    result = result_obj.model_dump() if hasattr(result_obj, "model_dump") else dict(result_obj)
    identity_after = certification_identity(
        spec,
        cfg,
        runner_provenance=runner_provenance,
        test_only=test_only,
    )
    identity_issues: list[str] = []
    if identity_after.get("candidate_hash") != identity_before.get("candidate_hash"):
        identity_issues.append("candidate_changed_during_official_certification")
    if identity_after.get("opponent_hash") != identity_before.get("opponent_hash"):
        identity_issues.append("opponent_changed_during_official_certification")
    if identity_after.get("platform_fingerprint") != identity_before.get("platform_fingerprint"):
        identity_issues.append("official_platform_policy_changed_during_certification")
    if spec.mode == "full":
        identity_issues.extend(_job_envelope_report_issues(result, job_envelope))
    if spec.mode != "full" and report_valid_for_spec(result, spec) and not identity_issues:
        key = _write_cache(spec, result, identity_before)
    return _status_for_result(
        spec,
        result,
        cache_hit=False,
        cache_key_value=key,
        identity=identity_before,
        request_started_ns=request_started_ns,
        identity_issues=identity_issues,
        opponent_selection=opponent_selection,
        job_envelope=job_envelope,
        test_only=test_only,
    )


def run_certification(
    spec: CertificationSpec,
    *,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    suite_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the production certification path with mandatory opponent governance."""
    if spec.mode == "full":
        raise RuntimeError(
            "formal full certification must run through official_certification_job"
        )
    return _run_production_certification(
        spec,
        config=config,
        force=force,
        suite_dir=suite_dir,
    )


def run_identity_bound_certification_job(
    spec: CertificationSpec,
    *,
    expected_identity: dict[str, Any],
    expected_opponent_selection: dict[str, Any] | None,
    suite_dir: str | Path,
    job_envelope: dict[str, Any],
    force: bool = False,
) -> dict[str, Any]:
    """Run a durable job without allowing its evidence identity to drift.

    Opponent governance is revalidated immediately before EXE work. If live
    policy would select a different artifact, the old job must fail and a new
    identity-bound job must be created; evidence is never attached to the old
    job id under a silently changed opponent.
    """
    validate_spec(spec)
    current_identity = certification_identity(spec)
    if current_identity != expected_identity:
        raise RuntimeError("official_job_runtime_identity_changed")
    from official_job_envelope import job_envelope_issues

    envelope_issues = job_envelope_issues(
        job_envelope,
        expected_candidate_hash=str(expected_identity.get("candidate_hash") or ""),
        expected_opponent_hash=str(expected_identity.get("opponent_hash") or ""),
    )
    if envelope_issues:
        raise RuntimeError(
            "official_job_envelope_invalid: " + ", ".join(envelope_issues)
        )
    resolved_spec, live_selection = resolve_managed_certification_spec(
        spec,
        exact_opponent_only=True,
    )
    if resolved_spec is None:
        failure = {
            "reason": (live_selection or {}).get("reason") or "selection_unavailable",
            "considered": (live_selection or {}).get("considered") or [],
        }
        raise RuntimeError(
            "official_job_opponent_no_longer_eligible: "
            + json.dumps(failure, ensure_ascii=True, sort_keys=True)[:2000]
        )
    if certification_identity(resolved_spec) != expected_identity:
        raise RuntimeError("official_job_opponent_selection_changed")
    expected_selection = stable_official_opponent_selection(expected_opponent_selection)
    current_selection = stable_official_opponent_selection(live_selection)
    expected_opponent = (expected_selection or {}).get("opponent") or {}
    live_opponent = (current_selection or {}).get("opponent") or {}
    if expected_opponent:
        expected_path = str(Path(str(expected_opponent.get("path") or "")).expanduser().resolve())
        live_path = str(Path(str(live_opponent.get("path") or "")).expanduser().resolve())
        if expected_path != live_path:
            raise RuntimeError("official_job_opponent_receipt_path_changed")
        expected_hash = str(expected_opponent.get("artifact_hash") or "")
        live_hash = str(live_opponent.get("artifact_hash") or "")
        if expected_hash and expected_hash != live_hash:
            raise RuntimeError("official_job_opponent_receipt_hash_changed")
        if expected_opponent.get("eligibility_receipt") != live_opponent.get("eligibility_receipt"):
            raise RuntimeError("official_job_opponent_eligibility_receipt_changed")
    if expected_selection != current_selection:
        raise RuntimeError("official_job_opponent_receipt_changed")
    return _run_certification_impl(
        spec,
        force=force,
        runner=_PRODUCTION_CERTIFICATION_RUNNER,
        runner_provenance=PRODUCTION_RUNNER_PROVENANCE,
        enforce_opponent_selection=False,
        opponent_selection=live_selection,
        suite_dir=suite_dir,
        job_envelope=job_envelope,
        _production_authority=_PRODUCTION_FULL_AUTHORITY,
    )


def _run_production_certification(
    spec: CertificationSpec,
    *,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    request_started_ns: int | None = None,
    suite_dir: str | Path | None = None,
) -> dict[str, Any]:
    runner = (
        _PRODUCTION_CERTIFICATION_RUNNER
        if spec.mode == "full"
        else run_official_acceptance_sync
    )
    return _run_certification_impl(
        spec,
        config=config,
        force=force,
        runner=runner,
        runner_provenance=PRODUCTION_RUNNER_PROVENANCE,
        enforce_opponent_selection=True,
        request_started_ns=request_started_ns,
        suite_dir=suite_dir,
        _production_authority=_PRODUCTION_FULL_AUTHORITY,
    )


def _run_certification_with_runner_for_test(
    spec: CertificationSpec,
    *,
    runner: Runner,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    request_started_ns: int | None = None,
    opponent_selection: dict[str, Any] | None = None,
    job_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inject a fake harness in unit tests without weakening the public API."""
    if "PYTEST_CURRENT_TEST" not in os.environ:
        raise RuntimeError("test certification runner is available only under pytest")
    if spec.mode == "full" and job_envelope is None:
        from official_job_envelope import build_job_envelope

        identity = certification_identity(
            spec,
            config,
            runner_provenance=TEST_ONLY_RUNNER_PROVENANCE,
            test_only=True,
        )
        request = {
            "job_id": "1" * 64,
            "request_digest": "2" * 64,
            "manager_sha256": "3" * 64,
            "identity": identity,
            "opponent_selection": stable_official_opponent_selection(opponent_selection),
            "source_v": None,
        }
        job_envelope = build_job_envelope(
            request,
            attempt=1,
            attempt_nonce="4" * 64,
            suite_dir=certification_root() / "pytest-suite",
        )
    def bound_test_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raw = runner(*args, **kwargs)
        payload = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
        if spec.mode == "full":
            report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
            report["job_envelope"] = job_envelope
            for receipt in report.get("rounds") or []:
                if isinstance(receipt, dict):
                    receipt["job_envelope"] = job_envelope
            payload["report"] = report
        return payload

    return _run_certification_impl(
        spec,
        config=config,
        force=force,
        runner=bound_test_runner,
        runner_provenance=TEST_ONLY_RUNNER_PROVENANCE,
        enforce_opponent_selection=False,
        request_started_ns=request_started_ns,
        opponent_selection=opponent_selection,
        job_envelope=job_envelope,
        test_only=True,
    )


def status_payload(candidate: str | Path) -> dict[str, Any]:
    payload = read_status(candidate)
    payload["compliance_verdict"] = official_compliance_verdict(payload)
    payload["certification_root"] = str(certification_root())
    return payload
