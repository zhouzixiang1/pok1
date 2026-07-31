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
    PUBLISHED_QUALITY_ADMISSION_KIND,
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
import official_certification_receipt_validation as _ocrv  # noqa: E402,F401  (receipt-validation cluster)
import official_certification_authority as _oca  # noqa: E402,F401  (authority/opponent-selection cluster)
import official_certification_runner as _ocr  # noqa: E402,F401  (runner/job-lifecycle cluster)


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
    # Normal manual full-v5 jobs carry a compact, content-bound admission from
    # the current checkpoint-owned dynamic quality/capability/probe ledger.
    # It is deliberately absent for the explicit first-strict control,
    # whose separate operator authorization is the sole admissibility path.
    quality_admission: dict[str, Any] | None = None


def normal_full_quality_admission_required(
    spec: CertificationSpec | dict[str, Any],
) -> bool:
    """Whether a strict normal full-v5 job must bind a quality admission.

    The pre-strict namespace remains readable as historical certificate data,
    but it is not an executable source of current certification authority.
    Every current strict normal full job must carry the exact
    checkpoint-owned receipt.  The explicit first-strict bootstrap is intentionally
    the only branch that takes the separate operator authorization path.
    A ``--published`` full job carries a published-kind admission (proving the
    candidate equals its published tag bytes) which is still required, but the
    structural check dispatches to the published-kind validator.
    """

    if isinstance(spec, CertificationSpec):
        mode = spec.mode
        bootstrap_control_id = spec.bootstrap_control_id
        candidate = spec.candidate
        admission = spec.quality_admission
    elif isinstance(spec, dict):
        mode = str(spec.get("mode") or "")
        bootstrap_control_id = spec.get("bootstrap_control_id")
        candidate = str(spec.get("candidate") or "")
        admission = spec.get("quality_admission")
    else:
        return False
    if mode != "full" or bootstrap_control_id is not None:
        return False
    try:
        version = parse_bot_version(Path(str(candidate)).name)
    except (TypeError, ValueError):
        version = None
    return bool(
        version is not None and int(version) >= FIRST_STRICT_POLICY_VERSION
    ) or (
        # A published-bot full job always binds its (published-kind) admission
        # regardless of version floor; the structural check below dispatches.
        isinstance(admission, dict)
        and admission.get("kind") == PUBLISHED_QUALITY_ADMISSION_KIND
    )


def normal_full_quality_admission_issues(
    spec: CertificationSpec | dict[str, Any],
) -> list[str]:
    """Return structural receipt errors for a strict normal full-v5 spec.

    This is deliberately a structural boundary only.  The job worker and
    harness immediately recompute the live checkpoint-owned receipt and reject
    any drift before the official EXE is touched.  A published-kind admission
    is validated by the published-kind structural validator instead.
    """

    if not normal_full_quality_admission_required(spec):
        return []
    if isinstance(spec, CertificationSpec):
        admission = spec.quality_admission
        candidate = spec.candidate
    else:
        admission = spec.get("quality_admission") if isinstance(spec, dict) else None
        candidate = str(spec.get("candidate") or "") if isinstance(spec, dict) else ""
    # Dispatch by admission kind: a published-bot full job proves its candidate
    # via the published tag, not the checkpoint-owned quality/capability/probe
    # ledger, so it uses a distinct structural validator.
    if isinstance(admission, dict) and admission.get("kind") == PUBLISHED_QUALITY_ADMISSION_KIND:
        from official_platform_harness import (
            formal_published_quality_admission_integrity_issues,
        )

        return formal_published_quality_admission_integrity_issues(
            admission,
            candidate=candidate,
        )
    from official_platform_harness import formal_quality_admission_integrity_issues

    return formal_quality_admission_integrity_issues(
        admission,
        candidate=candidate,
    )


def spec_record(spec: CertificationSpec) -> dict[str, Any]:
    """Serialize a spec without changing legacy v5 identity bytes.

    Omitting the ``None`` default keeps ordinary full-v5 identities compact;
    an explicit first-strict control authorization is identity-bearing.
    """
    record = asdict(spec)
    for optional_key in ("bootstrap_control_id", "quality_admission"):
        if record.get(optional_key) is None:
            record.pop(optional_key, None)
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
    quality_admission: dict[str, Any] | None = None,
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
        quality_admission=(
            dict(quality_admission)
            if isinstance(quality_admission, dict)
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
                "first-strict bootstrap control is valid only for "
                f"{bot_name(FIRST_STRICT_POLICY_VERSION)}"
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
    admission_issues = normal_full_quality_admission_issues(spec)
    if admission_issues:
        raise ValueError(
            "strict normal full certification requires a complete "
            "checkpoint-owned quality admission: "
            + ", ".join(admission_issues)
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
    """Delegate to official_certification_receipt_validation."""
    return _ocrv._max_thp_hands(receipt)


def _formal_thp_artifact_issues(
    receipt: dict[str, Any],
    *,
    expected_hands: int,
) -> list[str]:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv._formal_thp_artifact_issues(receipt, expected_hands=expected_hands)


def _formal_execution_issues(receipt: dict[str, Any]) -> list[str]:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv._formal_execution_issues(receipt)


def _log_target_reached(receipt: Any, target_hands: int) -> bool:
    if not isinstance(receipt, dict):
        return False
    return not round_completion_issues(receipt, target_hands)


def _full_v5_completion_issues(receipt: dict[str, Any]) -> list[str]:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv._full_v5_completion_issues(receipt)


def _same_resolved_path(left: Any, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(str(left)).expanduser().resolve() == Path(right).expanduser().resolve()
    except Exception:
        return False


def _full_evidence_artifact_issues(receipt: dict[str, Any]) -> list[str]:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv._full_evidence_artifact_issues(receipt)


def receipt_validation_issues(
    receipt: Any,
    spec: CertificationSpec,
    *,
    expected_kind: str | None = None,
    expected_index: int | None = None,
) -> list[str]:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv.receipt_validation_issues(receipt, spec, expected_kind=expected_kind, expected_index=expected_index)


def receipt_valid_for_spec(receipt: dict[str, Any], spec: CertificationSpec) -> bool:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv.receipt_valid_for_spec(receipt, spec)


def report_validation_issues(report: Any, spec: CertificationSpec) -> list[str]:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv.report_validation_issues(report, spec)


def report_valid_for_spec(report: dict[str, Any], spec: CertificationSpec) -> bool:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv.report_valid_for_spec(report, spec)


def _job_envelope_report_issues(
    report: dict[str, Any],
    job_envelope: dict[str, Any] | None,
) -> list[str]:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv._job_envelope_report_issues(report, job_envelope)


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


def read_status(
    candidate: str | Path,
    *,
    ledger_fresh: bool = True,
) -> dict[str, Any]:
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

        ledger = latest_authoritative_verdict(
            candidate_hash,
            fresh=ledger_fresh,
        )
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
        # The published certificate validator already bound this exact latest
        # signed verdict entry. Preserve that identity in the public status so
        # API/UI consumers do not have to reopen or reconstruct the ledger.
        return {
            **published,
            "official_verdict_ledger_entry": ledger_entry,
        }
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
    """Delegate to official_certification_receipt_validation."""
    return _ocrv._deterministic_status_receipt_issues(status, candidate=candidate)


def _issue_has_marker(issue: str, markers: tuple[str, ...]) -> bool:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv._issue_has_marker(issue, markers)


def _issues_have_protocol_violation(issues: list[str]) -> bool:
    return any(_issue_has_marker(issue, PARENT_BLOCKING_FAILURE_MARKERS) for issue in issues)


def official_compliance_verdict(status: dict[str, Any]) -> dict[str, Any]:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv.official_compliance_verdict(status)


def official_failure_blocks_parent(status: dict[str, Any]) -> bool:
    """Delegate to official_certification_receipt_validation."""
    return _ocrv.official_failure_blocks_parent(status)


def _certificate_payload_digest(record: dict[str, Any]) -> str:
    """Delegate to official_certification_authority."""
    return _oca._certificate_payload_digest(record)



def _build_deterministic_receipt(
    spec: CertificationSpec,
    evidence: dict[str, Any],
    evidence_path: Path,
    archive: dict[str, Any],
) -> dict[str, Any]:
    """Delegate to official_certification_authority."""
    return _oca._build_deterministic_receipt(spec, evidence, evidence_path, archive)



def _deterministic_receipt_issues(
    receipt: Any,
    spec: CertificationSpec,
    *,
    evidence_manifest: dict[str, Any],
    archive_receipt: dict[str, Any],
) -> list[str]:
    """Delegate to official_certification_authority."""
    return _oca._deterministic_receipt_issues(receipt, spec, evidence_manifest=evidence_manifest, archive_receipt=archive_receipt)



def _spec_from_mapping(data: dict[str, Any]) -> CertificationSpec:
    """Delegate to official_certification_authority."""
    return _oca._spec_from_mapping(data)



def _config_for_spec(
    spec: CertificationSpec,
    config: OfficialPlatformConfig | None = None,
) -> OfficialPlatformConfig:
    """Delegate to official_certification_authority."""
    return _oca._config_for_spec(spec, config)



def _identity_integrity_issues(identity: Any, spec: CertificationSpec) -> list[str]:
    """Delegate to official_certification_authority."""
    return _oca._identity_integrity_issues(identity, spec)



def _opponent_selection_issues(
    selection: Any,
    spec: CertificationSpec,
    identity: dict[str, Any],
    *,
    allow_consumed_bootstrap: bool = False,
    candidate_path: str | Path | None = None,
    _validated_ledger_entries: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Delegate to official_certification_authority."""
    return _oca._opponent_selection_issues(selection, spec, identity, allow_consumed_bootstrap=allow_consumed_bootstrap, candidate_path=candidate_path, _validated_ledger_entries=_validated_ledger_entries)



def _validate_portable_file_manifest(
    manifest: Any,
    *,
    label: str,
) -> tuple[Path | None, list[str], bool]:
    """Delegate to official_certification_authority."""
    return _oca._validate_portable_file_manifest(manifest, label=label)



def _validate_published_attestation_at_tag(
    candidate_path: Path,
    published_identity: dict[str, Any],
    expected_certificate_digest: str,
) -> list[str]:
    """Delegate to official_certification_authority."""
    return _oca._validate_published_attestation_at_tag(candidate_path, published_identity, expected_certificate_digest)



def certificate_validation(
    status: dict[str, Any],
    *,
    candidate: str | Path | None = None,
    config: OfficialPlatformConfig | None = None,
    require_published: bool = False,
    ledger_fresh: bool = True,
    _skip_ledger_check: bool = False,
    _validated_ledger_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Delegate to official_certification_authority."""
    return _oca.certificate_validation(status, candidate=candidate, config=config, require_published=require_published, ledger_fresh=ledger_fresh, _skip_ledger_check=_skip_ledger_check, _validated_ledger_entries=_validated_ledger_entries)



def publish_certificate_attestation(
    status: dict[str, Any],
    candidate: str | Path,
    *,
    config: OfficialPlatformConfig | None = None,
) -> dict[str, Any]:
    """Delegate to official_certification_authority."""
    return _oca.publish_certificate_attestation(status, candidate, config=config)



def official_full_certified(
    status: dict[str, Any],
    candidate: str | Path | None = None,
    *,
    config: OfficialPlatformConfig | None = None,
    require_published: bool = False,
    ledger_fresh: bool = True,
) -> bool:
    """Delegate to official_certification_authority."""
    return _oca.official_full_certified(status, candidate, config=config, require_published=require_published, ledger_fresh=ledger_fresh)



def official_certification_profile_projection(
    status: dict[str, Any],
    candidate: str | Path,
    *,
    require_published: bool = False,
) -> dict[str, Any]:
    """Delegate to official_certification_authority."""
    return _oca.official_certification_profile_projection(status, candidate, require_published=require_published)



def authoritative_verdict_status_issues(
    status: Any,
    *,
    _validated_ledger_entries: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Delegate to official_certification_authority."""
    return _oca.authoritative_verdict_status_issues(status, _validated_ledger_entries=_validated_ledger_entries)



def _official_verdict_ledger_issues() -> list[str]:
    """Delegate to official_certification_authority."""
    return _oca._official_verdict_ledger_issues()



def parent_eligible(candidate: str | Path) -> bool:
    """Delegate to official_certification_authority."""
    return _oca.parent_eligible(candidate)



def active_pool_eligible(candidate: str | Path) -> bool:
    """Delegate to official_certification_authority."""
    return _oca.active_pool_eligible(candidate)



def _digest_bound_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    """Delegate to official_certification_authority."""
    return _oca._digest_bound_receipt(payload)



def _official_certificate_opponent_receipt(
    candidate: str | Path,
    status: dict[str, Any],
) -> dict[str, Any]:
    """Delegate to official_certification_authority."""
    return _oca._official_certificate_opponent_receipt(candidate, status)



def stable_official_opponent_selection(
    selection: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Delegate to official_certification_authority."""
    return _oca.stable_official_opponent_selection(selection)



def official_opponent_eligibility(
    candidate: str | Path,
    *,
    allow_bootstrap_grandfather: bool = False,
    target_version: int | None = None,
    certified_alternatives: int | None = None,
) -> dict[str, Any]:
    """Delegate to official_certification_authority."""
    return _oca.official_opponent_eligibility(candidate, allow_bootstrap_grandfather=allow_bootstrap_grandfather, target_version=target_version, certified_alternatives=certified_alternatives)



def _bot_path_from_token(token: str | Path) -> Path:
    """Delegate to official_certification_authority."""
    return _oca._bot_path_from_token(token)



def _same_bot_path(a: Path, b: Path) -> bool:
    """Delegate to official_certification_authority."""
    return _oca._same_bot_path(a, b)



def select_official_opponent(
    candidate: str | Path,
    active_bots: list[str] | tuple[str, ...] | None = None,
    *,
    preferred: str | Path | None = None,
    allow_bootstrap_grandfather: bool = False,
) -> dict[str, Any]:
    """Delegate to official_certification_authority."""
    return _oca.select_official_opponent(candidate, active_bots, preferred=preferred, allow_bootstrap_grandfather=allow_bootstrap_grandfather)



def resolve_managed_certification_spec(
    spec: CertificationSpec,
    *,
    exact_opponent_only: bool = False,
) -> tuple[CertificationSpec | None, dict[str, Any] | None]:
    """Delegate to official_certification_authority."""
    return _oca.resolve_managed_certification_spec(spec, exact_opponent_only=exact_opponent_only)

def official_lock_busy(config: OfficialPlatformConfig | None = None) -> bool:
    """Delegate to official_certification_runner."""
    return _ocr.official_lock_busy(config)



def _evidence_path_for_result(spec: CertificationSpec, summary: dict[str, Any], cache_key_value: str) -> Path:
    """Delegate to official_certification_runner."""
    return _ocr._evidence_path_for_result(spec, summary, cache_key_value)



def _official_llm_analysis_enabled() -> bool:
    """Delegate to official_certification_runner."""
    return _ocr._official_llm_analysis_enabled()



def _short_text(value: Any, limit: int = 1200) -> str:
    """Delegate to official_certification_runner."""
    return _ocr._short_text(value, limit)



def _write_certificate_record(
    spec: CertificationSpec,
    identity: dict[str, Any],
    evidence_extra: dict[str, Any],
    cache_key_value: str,
    opponent_selection: dict[str, Any] | None = None,
    job_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Delegate to official_certification_runner."""
    return _ocr._write_certificate_record(spec, identity, evidence_extra, cache_key_value, opponent_selection, job_envelope)



def official_feedback_summary(*, limit: int = 8, max_chars: int = 6000) -> str:
    """Delegate to official_certification_runner."""
    return _ocr.official_feedback_summary(limit=limit, max_chars=max_chars)



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
    """Delegate to official_certification_runner."""
    return _ocr._status_for_result(spec, result, cache_hit=cache_hit, cache_key_value=cache_key_value, identity=identity, request_started_ns=request_started_ns, identity_issues=identity_issues, opponent_selection=opponent_selection, job_envelope=job_envelope, test_only=test_only)



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
    """Delegate to official_certification_runner."""
    return _ocr._run_certification_impl(spec, config=config, force=force, runner=runner, runner_provenance=runner_provenance, enforce_opponent_selection=enforce_opponent_selection, request_started_ns=request_started_ns, opponent_selection=opponent_selection, suite_dir=suite_dir, job_envelope=job_envelope, test_only=test_only, _production_authority=_production_authority)



def run_certification(
    spec: CertificationSpec,
    *,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    suite_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Delegate to official_certification_runner."""
    return _ocr.run_certification(spec, config=config, force=force, suite_dir=suite_dir)



def run_identity_bound_certification_job(
    spec: CertificationSpec,
    *,
    expected_identity: dict[str, Any],
    expected_opponent_selection: dict[str, Any] | None,
    suite_dir: str | Path,
    job_envelope: dict[str, Any],
    force: bool = False,
) -> dict[str, Any]:
    """Delegate to official_certification_runner."""
    return _ocr.run_identity_bound_certification_job(spec, expected_identity=expected_identity, expected_opponent_selection=expected_opponent_selection, suite_dir=suite_dir, job_envelope=job_envelope, force=force)



def _run_production_certification(
    spec: CertificationSpec,
    *,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    request_started_ns: int | None = None,
    suite_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Delegate to official_certification_runner."""
    return _ocr._run_production_certification(spec, config=config, force=force, request_started_ns=request_started_ns, suite_dir=suite_dir)



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
    """Delegate to official_certification_runner."""
    return _ocr._run_certification_with_runner_for_test(spec, runner=runner, config=config, force=force, request_started_ns=request_started_ns, opponent_selection=opponent_selection, job_envelope=job_envelope)



def status_payload(candidate: str | Path) -> dict[str, Any]:
    """Delegate to official_certification_runner."""
    return _ocr.status_payload(candidate)
