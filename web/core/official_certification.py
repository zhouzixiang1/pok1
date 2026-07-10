"""Official EXE certification status, cache, and queue helpers.

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
import time
from typing import Any, Callable, Literal

from bot_artifact import canonical_digest, hash_path, published_bot_identity
from bot_namespace import bot_name, parse_bot_version
from official_eligibility import grandfather_eligibility
from official_platform_harness import (
    OfficialPlatformConfig,
    _copy_config,
    run_official_acceptance_sync,
)
from official_evidence import build_official_evidence_bundle


ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "web" / "core" / "results"
DEFAULT_CERT_DIR = RESULTS_DIR / "official_certification"
HARNESS_PATH = ROOT / "web" / "core" / "official_platform_harness.py"
SERVICE_PATH = Path(__file__).resolve()

CertificationMode = Literal["smoke", "compliance", "full"]
FULL_POLICY_ID = "official-full-v2"
CERTIFICATE_SCHEMA_VERSION = 2

STATUS_LOCAL_PASS = "local-pass"
STATUS_SMOKE_PASS = "official-smoke-pass"
STATUS_COMPLIANCE_PASS = "official-compliance-pass"
STATUS_PENDING = "official-pending"
STATUS_CERTIFIED = "official-certified"
STATUS_GRANDFATHERED = "official-grandfathered"
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


Runner = Callable[..., Any]


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


def queue_path() -> Path:
    return certification_root() / "queue.jsonl"


def queue_lock_path() -> Path:
    return certification_root() / "queue.lock"


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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@contextmanager
def _queue_lock(*, blocking: bool = True):
    queue_lock_path().parent.mkdir(parents=True, exist_ok=True)
    with queue_lock_path().open("a+", encoding="utf-8") as lock_fp:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(lock_fp.fileno(), flags)
        try:
            yield
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


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
                "full certification must use official-full-v2 with "
                "5 self + 3 opponent rounds of 70 hands"
            )


def _config_fingerprint(config: OfficialPlatformConfig) -> dict[str, Any]:
    return {
        "exe_path": str(config.exe_path),
        "exe_sha256": _file_sha256(config.exe_path) if config.exe_path.exists() else "missing",
        "harness_sha256": _file_sha256(HARNESS_PATH) if HARNESS_PATH.exists() else "missing",
        "service_sha256": _file_sha256(SERVICE_PATH) if SERVICE_PATH.exists() else "missing",
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
        "spec": asdict(spec),
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


def _max_thp_hands(receipt: dict[str, Any]) -> int:
    summaries = receipt.get("artifacts", {}).get("thp_summaries", []) or []
    values = []
    for item in summaries:
        try:
            values.append(int(item.get("hand_records", 0) or 0))
        except Exception:
            values.append(0)
    return max(values, default=0)


def _log_target_reached(receipt: dict[str, Any], target_hands: int) -> bool:
    summary = receipt.get("log_summary") or {}
    try:
        hands_started = int(summary.get("hands_started_min", 0) or 0)
        settlements = int(summary.get("settlements_min", 0) or 0)
    except Exception:
        return False
    return hands_started >= target_hands and settlements >= max(0, target_hands - 1)


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
    receipt: dict[str, Any],
    spec: CertificationSpec,
    *,
    expected_kind: str | None = None,
    expected_index: int | None = None,
) -> list[str]:
    issues: list[str] = []
    if receipt.get("passed") is not True:
        issues.append("receipt_not_passed")
    receipt_issues = receipt.get("issues") or []
    if receipt_issues:
        issues.extend(str(issue) for issue in receipt_issues)
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
        if not _same_resolved_path(bot_a.get("path"), spec.candidate):
            issues.append("opponent_round_candidate_identity_mismatch")
        if not _same_resolved_path(bot_b.get("path"), spec.opponent):
            issues.append("opponent_round_opponent_identity_mismatch")

    thp_hands = _max_thp_hands(receipt)
    if spec.mode == "full" or spec.target_hands >= 70:
        issues.extend(_full_evidence_artifact_issues(receipt))
        if thp_hands < spec.target_hands:
            issues.append(f"thp_incomplete_for_full_certification: hands={thp_hands} target={spec.target_hands}")
            summary = receipt.get("log_summary") or {}
            try:
                hands_started = int(summary.get("hands_started_min", 0) or 0)
                settlements = int(summary.get("settlements_min", 0) or 0)
            except Exception:
                hands_started = 0
                settlements = 0
            if hands_started > 0 and hands_started < spec.target_hands:
                net_abs = 0
                for side in ("bot_a", "bot_b"):
                    side_summary = summary.get(side) if isinstance(summary.get(side), dict) else {}
                    try:
                        net_abs = max(net_abs, abs(int(side_summary.get("net_chips", 0) or 0)))
                    except Exception:
                        pass
                issues.append(
                    "official_full_round_incomplete_after_progress: "
                    f"hands_started={hands_started} settlements={settlements} "
                    f"target={spec.target_hands} max_abs_net_chips={net_abs}"
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


def report_validation_issues(report: dict[str, Any], spec: CertificationSpec) -> list[str]:
    try:
        validate_spec(spec)
    except Exception as exc:
        return [f"invalid_certification_spec:{type(exc).__name__}:{str(exc)[:300]}"]
    issues: list[str] = []
    if report.get("passed") is not True:
        issues.append("report_not_passed")
    report_issues = report.get("issues") or []
    if report_issues:
        issues.extend(str(issue) for issue in report_issues)
    rounds = report.get("report", {}).get("rounds", []) or []
    expected = spec.self_play_rounds + spec.opponent_rounds
    if len(rounds) != expected:
        issues.append(f"round_count_mismatch: rounds={len(rounds)} expected={expected}")
    if spec.mode == "full":
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
            dict(receipt),
            spec,
            expected_kind=expected_kind,
            expected_index=expected_index,
        )
        issues.extend(f"round_{index}: {issue}" for issue in receipt_issues)
    return issues


def report_valid_for_spec(report: dict[str, Any], spec: CertificationSpec) -> bool:
    return not report_validation_issues(report, spec)


def _cache_hit(spec: CertificationSpec, config: OfficialPlatformConfig | None = None) -> dict[str, Any] | None:
    identity = certification_identity(spec, config)
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
        "spec": asdict(spec),
        "identity": identity,
        "result": result,
    }
    _write_json(_cache_file(key), payload)
    return key


def _status_path(label: str) -> Path:
    return status_dir() / f"{label}.json"


def read_status(candidate: str | Path) -> dict[str, Any]:
    label = _safe_label(candidate)
    payload = _read_json(_status_path(label)) or {}
    if payload:
        return payload
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


def record_grandfathered(
    candidate: str | Path,
    *,
    reason: str,
    source: str = "official_transition",
) -> dict[str, Any]:
    """Reject mutable status-based grandfathering.

    Transitional grants are policy, not platform evidence. They must be added
    to ``official_grandfathering.json`` with an immutable artifact hash.
    """
    raise RuntimeError(
        "mutable official grandfather status is disabled; add a content-bound "
        "role grant to web/core/official_grandfathering.json"
    )


def _official_issue_strings(status: dict[str, Any]) -> list[str]:
    return [str(issue) for issue in (status.get("issues") or [])]


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
    evidence_blocking = bool(evidence_summary.get("blocking")) and not bool(evidence_summary.get("inconclusive"))
    if evidence_blocking:
        verdict_class = evidence_class or "official_evidence_blocking"
        if evidence_class == "protocol":
            verdict_class = "protocol_violation"
        elif evidence_class == "obvious_decision_error":
            verdict_class = "official_full_incomplete"
        return {
            "ok": False,
            "blocking": True,
            "classification": verdict_class,
            "inconclusive": False,
            "violation": bool(evidence_summary.get("violation", True)),
            "issues": issues,
            "violation_issues": issues,
            "official_evidence_summary": evidence_summary,
        }
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
    if status_value == STATUS_GRANDFATHERED:
        return {
            "ok": True,
            "blocking": False,
            "classification": "grandfathered",
            "inconclusive": False,
            "violation": False,
            "issues": issues,
            "grandfathered": True,
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

    violation_issues = [
        issue for issue in issues
        if _issue_has_marker(issue, PARENT_BLOCKING_FAILURE_MARKERS)
    ]
    decision_issues = [
        issue for issue in issues
        if _issue_has_marker(issue, OFFICIAL_DECISION_FAILURE_MARKERS)
    ]
    inconclusive_issues = [
        issue for issue in issues
        if _issue_has_marker(issue, COMPLIANCE_INCONCLUSIVE_FAILURE_MARKERS)
    ]
    if violation_issues:
        if decision_issues and len(decision_issues) == len(violation_issues):
            return {
                "ok": False,
                "blocking": True,
                "classification": "official_full_incomplete",
                "inconclusive": False,
                "violation": False,
                "issues": issues,
                "violation_issues": violation_issues,
                "decision_issues": decision_issues,
            }
        return {
            "ok": False,
            "blocking": True,
            "classification": "protocol_violation",
            "inconclusive": False,
            "violation": True,
            "issues": issues,
            "violation_issues": violation_issues,
        }
    return {
        "ok": True,
        "blocking": False,
        "classification": "inconclusive",
        "inconclusive": True,
        "violation": False,
        "issues": issues,
        "inconclusive_issues": inconclusive_issues or issues,
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


def _spec_from_mapping(data: dict[str, Any]) -> CertificationSpec:
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


def certificate_validation(
    status: dict[str, Any],
    *,
    candidate: str | Path | None = None,
    config: OfficialPlatformConfig | None = None,
    require_published: bool = False,
) -> dict[str, Any]:
    issues: list[str] = []
    path_value = status.get("certificate_path")
    record = _read_json(Path(str(path_value))) if path_value else None
    if not isinstance(record, dict):
        return {"valid": False, "issues": ["content_bound_certificate_missing"]}
    digest = str(record.get("certificate_digest") or "")
    if not digest or digest != _certificate_payload_digest(record):
        issues.append("certificate_digest_mismatch")
    if digest != str(status.get("certificate_digest") or ""):
        issues.append("status_certificate_digest_mismatch")
    try:
        spec = _spec_from_mapping(record.get("spec") or {})
    except Exception as exc:
        return {
            "valid": False,
            "issues": [f"certificate_spec_invalid:{type(exc).__name__}:{str(exc)[:200]}"],
        }
    current_identity = certification_identity(spec, _config_for_spec(spec, config))
    if record.get("identity") != current_identity:
        issues.append("certificate_identity_stale")
    status_identity = status.get("certification_identity") or {}
    if status_identity != current_identity:
        issues.append("status_identity_stale")
    candidate_path = Path(candidate).expanduser().resolve() if candidate is not None else Path(spec.candidate)
    try:
        if hash_path(candidate_path) != current_identity.get("candidate_hash"):
            issues.append("candidate_artifact_hash_mismatch")
    except Exception as exc:
        issues.append(
            f"candidate_artifact_integrity_error:{type(exc).__name__}:{str(exc)[:160]}"
        )
    evidence_path, evidence_issues = _validate_certificate_file_manifest(
        record.get("evidence"),
        label="evidence",
    )
    issues.extend(evidence_issues)
    if evidence_path is not None and not evidence_issues:
        issues.extend(_validate_retained_evidence_artifacts(evidence_path))
    _analysis_path, analysis_issues = _validate_certificate_file_manifest(
        record.get("llm_analysis"),
        label="llm_analysis",
    )
    issues.extend(analysis_issues)
    llm_summary = (record.get("llm_analysis") or {}).get("summary") or {}
    try:
        llm_confidence = float(llm_summary.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        llm_confidence = 0.0
    if not (
        llm_summary.get("analysis_source") == "llm"
        and llm_summary.get("compliance_verdict") == "pass"
        and llm_confidence >= 0.5
    ):
        issues.append("certificate_llm_analysis_not_complete")
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
    return {
        "valid": not issues,
        "issues": list(dict.fromkeys(issues)),
        "certificate_digest": digest,
        "spec": asdict(spec),
        "identity": current_identity,
    }


def official_full_certified(
    status: dict[str, Any],
    candidate: str | Path | None = None,
    *,
    config: OfficialPlatformConfig | None = None,
    require_published: bool = False,
) -> bool:
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
    return bool(validation.get("valid"))


def parent_eligible(candidate: str | Path) -> bool:
    status = read_status(candidate)
    if official_failure_blocks_parent(status):
        return False
    if official_full_certified(status, candidate, require_published=True):
        return True
    return bool(
        grandfather_eligibility(candidate, "parent_source").get("eligible")
    )


def active_pool_eligible(candidate: str | Path) -> bool:
    status = read_status(candidate)
    if official_failure_blocks_parent(status):
        return False
    if official_full_certified(status, candidate, require_published=True):
        return True
    parent_grant = grandfather_eligibility(candidate, "parent_source")
    rating_grant = grandfather_eligibility(candidate, "rating_pool")
    return bool(parent_grant.get("eligible") and rating_grant.get("eligible"))


def official_opponent_eligibility(
    candidate: str | Path,
    *,
    allow_bootstrap_grandfather: bool = False,
    target_version: int | None = None,
) -> dict[str, Any]:
    """Return content-bound eligibility for an official-EXE opponent."""
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
    if official_full_certified(status, candidate, require_published=True):
        reason = "official_certified"
        priority = 0
    else:
        grant = grandfather_eligibility(
            candidate,
            "official_opponent",
            target_version=target_version,
        )
        if not grant.get("eligible"):
            return {
                "eligible": False,
                "reason": grant.get("reason") or "not_official_certified",
                "status": status.get("status"),
                "mode": status.get("mode"),
                "verdict": verdict,
                "grant": grant,
                "bootstrap_requested_but_disabled": bool(allow_bootstrap_grandfather),
            }
        reason = "content_bound_grandfather_grant"
        priority = 1
    return {
        "eligible": True,
        "reason": reason,
        "priority": priority,
        "status": status.get("status"),
        "mode": status.get("mode"),
        "verdict": verdict,
        "grandfathered": reason == "content_bound_grandfather_grant",
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
    active_bots: list[str] | tuple[str, ...],
    *,
    preferred: str | Path | None = None,
    allow_bootstrap_grandfather: bool = False,
) -> dict[str, Any]:
    candidate_path = _bot_path_from_token(candidate)
    target_version = parse_bot_version(candidate_path.name)
    try:
        from evolution_infra import load_reaped_bot_versions

        reaped_versions = load_reaped_bot_versions()
        lifecycle_error = ""
    except Exception as exc:
        reaped_versions = None
        lifecycle_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    raw_tokens: list[str | Path] = []
    if preferred:
        raw_tokens.append(preferred)
    raw_tokens.extend(active_bots)

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

            native_errors = check_native_contract(path)
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
        )
        item = {
            "bot": name,
            "path": str(path),
            "artifact_hash": identity.get("artifact_hash"),
            "tag": identity.get("tag"),
            "tag_object": identity.get("tag_object"),
            **eligibility,
        }
        considered.append(item)

    eligible = [item for item in considered if item.get("eligible")]
    if not eligible:
        return {
            "selected": False,
            "reason": "no_official_eligible_opponent",
            "candidate": str(candidate_path),
            "considered": considered,
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
    }


def official_lock_busy(config: OfficialPlatformConfig | None = None) -> bool:
    cfg = config or OfficialPlatformConfig()
    cfg.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg.lock_path.open("a+", encoding="utf-8") as lock_fp:
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def _read_queue_entries() -> list[dict[str, Any]]:
    path = queue_path()
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("status", "pending") == "pending":
            entries.append(payload)
    return entries


def _write_queue_entries(entries: list[dict[str, Any]]) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if entries:
        text = "\n".join(json.dumps(_jsonable(entry), ensure_ascii=False) for entry in entries) + "\n"
    else:
        text = ""
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _spec_from_queue_entry(entry: dict[str, Any]) -> CertificationSpec:
    data = entry.get("spec") or {}
    mode = data.get("mode")
    if mode not in MODE_CONFIG:
        raise ValueError(f"invalid certification mode in queue entry: {mode!r}")
    spec = CertificationSpec(
        mode=mode,
        policy_id=str(
            data.get("policy_id")
            or (FULL_POLICY_ID if mode == "full" else f"official-{mode}-v1")
        ),
        candidate=str(data["candidate"]),
        opponent=str(data["opponent"]) if data.get("opponent") else None,
        self_play_rounds=int(data["self_play_rounds"]),
        opponent_rounds=int(data["opponent_rounds"]),
        target_hands=int(data["target_hands"]),
        round_timeout_sec=float(data["round_timeout_sec"]),
        no_progress_timeout_sec=float(data["no_progress_timeout_sec"]),
    )
    validate_spec(spec)
    return spec


def queue_snapshot() -> dict[str, Any]:
    with _queue_lock(blocking=True):
        entries = _read_queue_entries()
    return {
        "queue_path": str(queue_path()),
        "pending": len(entries),
        "entries": entries,
    }


def enqueue_certification(
    spec: CertificationSpec,
    *,
    reason: str = "requested",
    config: OfficialPlatformConfig | None = None,
) -> dict[str, Any]:
    validate_spec(spec)
    identity = certification_identity(spec, config)
    key = cache_key(spec, config)
    current = read_status(spec.candidate)
    if current.get("cache_key") == key and current.get("status") in {
        STATUS_SMOKE_PASS,
        STATUS_COMPLIANCE_PASS,
        STATUS_CERTIFIED,
    }:
        return current
    if current.get("status") == STATUS_CERTIFIED and spec.mode in {"smoke", "compliance"}:
        return current
    if current.get("status") == STATUS_COMPLIANCE_PASS and spec.mode == "smoke":
        return current
    entry = {
        "cache_key": key,
        "queued_at": now_iso(),
        "reason": reason,
        "spec": asdict(spec),
        "status": "pending",
    }
    with _queue_lock(blocking=True):
        entries = _read_queue_entries()
        if not any(item.get("cache_key") == key for item in entries):
            entries.append(entry)
            _write_queue_entries(entries)
    return write_status(
        spec.candidate,
        STATUS_PENDING,
        mode=spec.mode,
        policy_id=spec.policy_id,
        cache_key=key,
        certification_identity=identity,
        reason=reason,
        queued=True,
        issues=[],
    )


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
) -> dict[str, Any]:
    evidence_path = Path(str(evidence_extra.get("official_evidence_path") or ""))
    analysis_path = Path(str(evidence_extra.get("official_llm_analysis_path") or ""))
    payload = {
        "schema_version": CERTIFICATE_SCHEMA_VERSION,
        "issued_at": now_iso(),
        "policy_id": spec.policy_id,
        "mode": spec.mode,
        "spec": asdict(spec),
        "identity": identity,
        "cache_key": cache_key_value,
        "evidence": {
            **_certificate_file_manifest(evidence_path, label="official evidence"),
            "summary": evidence_extra.get("official_evidence_summary") or {},
        },
        "llm_analysis": {
            **_certificate_file_manifest(analysis_path, label="official LLM analysis"),
            "summary": evidence_extra.get("official_llm_analysis_summary") or {},
        },
        "strength_evaluation": "not_applicable",
    }
    digest = canonical_digest(payload)
    path = (
        certificate_dir()
        / str(identity.get("candidate_hash") or "missing")
        / f"{digest}.json"
    )
    record = {
        **payload,
        "certificate_digest": digest,
        "certificate_path": str(path),
    }
    _write_json(path, record)
    return record


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
            or payload.get("status") in {STATUS_FAILED, STATUS_INCONCLUSIVE, STATUS_GRANDFATHERED}
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
    identity_issues: list[str] | None = None,
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
        elif deterministic.get("inconclusive") and status in {
            STATUS_CERTIFIED,
            STATUS_COMPLIANCE_PASS,
            STATUS_SMOKE_PASS,
        }:
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
                "rounds_run": deterministic.get("rounds_run"),
                "target_hands": deterministic.get("target_hands"),
                "strength_evaluation": "not_applicable",
            },
        }
        analysis_path = evidence_path.with_name("llm_official_analysis.json")
        if _official_llm_analysis_enabled():
            from official_llm_analysis import run_official_llm_analysis_sync

            analysis = run_official_llm_analysis_sync(evidence, output_path=analysis_path)
        else:
            from official_llm_analysis import safe_default_analysis

            analysis = safe_default_analysis(evidence, reason="llm_disabled")
            analysis["analysis_path"] = str(analysis_path)
            _write_json(analysis_path, analysis)
        evidence_extra["official_llm_analysis_path"] = str(analysis_path)
        evidence_extra["official_llm_analysis_summary"] = {
            "compliance_verdict": analysis.get("compliance_verdict"),
            "failure_class": analysis.get("failure_class"),
            "blocking": analysis.get("blocking"),
            "confidence": analysis.get("confidence"),
            "analysis_source": analysis.get("analysis_source"),
            "repair_guidance": _short_text(analysis.get("repair_guidance"), 1200),
            "prompt_feedback": _short_text(analysis.get("prompt_feedback"), 1200),
            "strength_evaluation": "not_applicable",
        }
        evidence_extra["official_llm_repair_guidance"] = _short_text(
            analysis.get("repair_guidance"),
            2000,
        )
        evidence_extra["official_llm_prompt_feedback"] = _short_text(
            analysis.get("prompt_feedback"),
            2000,
        )
        if spec.mode == "full" and status != STATUS_FAILED:
            try:
                analysis_confidence = float(analysis.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                analysis_confidence = 0.0
            llm_complete = (
                analysis.get("analysis_source") == "llm"
                and analysis.get("compliance_verdict") == "pass"
                and analysis_confidence >= 0.5
            )
            if not llm_complete:
                issues = list(dict.fromkeys([
                    *issues,
                    "official_full_llm_analysis_incomplete: "
                    f"source={analysis.get('analysis_source')} "
                    f"verdict={analysis.get('compliance_verdict')} "
                    f"confidence={analysis_confidence:.3f}",
                ]))
                status = STATUS_INCONCLUSIVE
    except Exception as exc:
        issue = f"official_evidence_error: {type(exc).__name__}: {str(exc)[:300]}"
        issues = list(dict.fromkeys([*issues, issue]))
        status = STATUS_INCONCLUSIVE
        evidence_extra = {
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
            )
            certificate_extra = {
                "certificate_schema_version": record.get("schema_version"),
                "certificate_digest": record.get("certificate_digest"),
                "certificate_path": record.get("certificate_path"),
            }
        except Exception as exc:
            issues = list(dict.fromkeys([
                *issues,
                f"official_certificate_artifact_error:{type(exc).__name__}:{str(exc)[:240]}",
            ]))
            status = STATUS_INCONCLUSIVE
    return write_status(
        spec.candidate,
        status,
        mode=spec.mode,
        policy_id=spec.policy_id,
        cache_hit=cache_hit,
        cache_key=cache_key_value,
        certification_identity=identity,
        summary=summary,
        issues=issues,
        result=result,
        **evidence_extra,
        **certificate_extra,
    )


def run_certification(
    spec: CertificationSpec,
    *,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    queue_on_busy: bool = True,
    runner: Runner = run_official_acceptance_sync,
) -> dict[str, Any]:
    validate_spec(spec)
    cfg = config or OfficialPlatformConfig()
    cfg = _copy_config(
        cfg,
        round_timeout_sec=spec.round_timeout_sec,
        no_progress_timeout_sec=spec.no_progress_timeout_sec,
        results_dir=certification_root() / spec.mode,
    )
    identity_before = certification_identity(spec, cfg)
    key = str(identity_before["identity_digest"])
    if not force:
        cached = _cache_hit(spec, cfg)
        if cached:
            return _status_for_result(
                spec,
                cached["result"],
                cache_hit=True,
                cache_key_value=key,
                identity=identity_before,
            )
    if queue_on_busy and official_lock_busy(cfg):
        return enqueue_certification(spec, reason="official_platform_busy", config=cfg)

    result_obj = runner(
        spec.candidate,
        opponent=spec.opponent,
        self_play_rounds=spec.self_play_rounds,
        opponent_rounds=spec.opponent_rounds,
        target_hands=spec.target_hands,
        config=cfg,
    )
    result = result_obj.model_dump() if hasattr(result_obj, "model_dump") else dict(result_obj)
    identity_after = certification_identity(spec, cfg)
    identity_issues: list[str] = []
    if identity_after.get("candidate_hash") != identity_before.get("candidate_hash"):
        identity_issues.append("candidate_changed_during_official_certification")
    if identity_after.get("opponent_hash") != identity_before.get("opponent_hash"):
        identity_issues.append("opponent_changed_during_official_certification")
    if identity_after.get("platform_fingerprint") != identity_before.get("platform_fingerprint"):
        identity_issues.append("official_platform_policy_changed_during_certification")
    if report_valid_for_spec(result, spec) and not identity_issues:
        key = _write_cache(spec, result, identity_before)
    return _status_for_result(
        spec,
        result,
        cache_hit=False,
        cache_key_value=key,
        identity=identity_before,
        identity_issues=identity_issues,
    )


def process_certification_queue(
    *,
    limit: int = 1,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    runner: Runner = run_official_acceptance_sync,
) -> dict[str, Any]:
    processed: list[dict[str, Any]] = []
    errors: list[str] = []
    limit = max(1, int(limit))
    selected: list[dict[str, Any]] = []
    try:
        with _queue_lock(blocking=False):
            entries = _read_queue_entries()
            if not entries:
                return {"processed": 0, "remaining": 0, "lock_busy": False, "results": []}
            if official_lock_busy(config):
                return {
                    "processed": 0,
                    "remaining": len(entries),
                    "lock_busy": True,
                    "results": [],
                }
            selected = entries[:limit]
            _write_queue_entries(entries[limit:])
    except BlockingIOError:
        return {"processed": 0, "remaining": None, "lock_busy": True, "results": []}

    requeue: list[dict[str, Any]] = []
    for entry in selected:
        try:
            spec = _spec_from_queue_entry(entry)
            result = run_certification(
                spec,
                config=config,
                force=force,
                queue_on_busy=False,
                runner=runner,
            )
            processed.append({
                "cache_key": entry.get("cache_key"),
                "candidate": spec.candidate,
                "mode": spec.mode,
                "status": result.get("status"),
            })
            if result.get("status") == STATUS_PENDING:
                requeue.append(entry)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            requeue.append(entry)

    if requeue:
        with _queue_lock(blocking=True):
            current = _read_queue_entries()
            existing_keys = {item.get("cache_key") for item in current}
            merged = [entry for entry in requeue if entry.get("cache_key") not in existing_keys] + current
            _write_queue_entries(merged)

    with _queue_lock(blocking=True):
        remaining_count = len(_read_queue_entries())
    return {
        "processed": len(processed),
        "remaining": remaining_count,
        "lock_busy": False,
        "results": processed,
        "errors": errors,
    }


def status_payload(candidate: str | Path) -> dict[str, Any]:
    payload = read_status(candidate)
    payload["compliance_verdict"] = official_compliance_verdict(payload)
    payload["certification_root"] = str(certification_root())
    return payload
