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

from bot_namespace import bot_name, parse_bot_version
from official_platform_harness import (
    OfficialPlatformConfig,
    _copy_config,
    run_official_acceptance_sync,
)


ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "web" / "core" / "results"
DEFAULT_CERT_DIR = RESULTS_DIR / "official_certification"
HARNESS_PATH = ROOT / "web" / "core" / "official_platform_harness.py"
SERVICE_PATH = Path(__file__).resolve()

CertificationMode = Literal["smoke", "compliance", "full"]

STATUS_LOCAL_PASS = "local-pass"
STATUS_SMOKE_PASS = "official-smoke-pass"
STATUS_COMPLIANCE_PASS = "official-compliance-pass"
STATUS_PENDING = "official-pending"
STATUS_CERTIFIED = "official-certified"
STATUS_INCONCLUSIVE = "official-inconclusive"
STATUS_FAILED = "official-failed"

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


def hash_path(path_or_token: str | Path) -> str:
    """Hash bot source or a single file, excluding runtime/cache artifacts."""
    path = Path(path_or_token).expanduser().resolve()
    h = hashlib.sha256()
    if path.is_file():
        h.update(path.name.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update(path.read_bytes())
        return h.hexdigest()
    if not path.exists():
        h.update(f"missing:{path}".encode("utf-8", "surrogateescape"))
        return h.hexdigest()

    excluded_names = {".completed"}
    excluded_suffixes = {".pyc", ".pyo"}
    files = []
    for item in path.rglob("*"):
        if not item.is_file() or item.is_symlink():
            continue
        rel = item.relative_to(path)
        if "__pycache__" in rel.parts:
            continue
        if item.name in excluded_names or item.suffix in excluded_suffixes:
            continue
        if item.name.startswith("."):
            continue
        files.append((str(rel).replace(os.sep, "/"), item))
    for rel, item in sorted(files):
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update(item.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


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
    return CertificationSpec(
        mode=mode,
        candidate=str(Path(candidate).expanduser().resolve()),
        opponent=str(Path(opponent).expanduser().resolve()) if opponent else None,
        self_play_rounds=int(self_play_rounds if self_play_rounds is not None else defaults["self_play_rounds"]),
        opponent_rounds=int(opponent_rounds if opponent_rounds is not None else defaults["opponent_rounds"]),
        target_hands=max(1, min(70, int(target_hands if target_hands is not None else defaults["target_hands"]))),
        round_timeout_sec=float(round_timeout_sec if round_timeout_sec is not None else defaults["round_timeout_sec"]),
        no_progress_timeout_sec=float(
            no_progress_timeout_sec if no_progress_timeout_sec is not None else defaults["no_progress_timeout_sec"]
        ),
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
    }


def cache_key(spec: CertificationSpec, config: OfficialPlatformConfig | None = None) -> str:
    cfg = config or OfficialPlatformConfig()
    payload = {
        "schema": 1,
        "spec": asdict(spec),
        "candidate_hash": hash_path(spec.candidate),
        "opponent_hash": hash_path(spec.opponent) if spec.opponent else None,
        "platform": _config_fingerprint(cfg),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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


def receipt_validation_issues(receipt: dict[str, Any], spec: CertificationSpec) -> list[str]:
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

    thp_hands = _max_thp_hands(receipt)
    if spec.mode == "full" or spec.target_hands >= 70:
        if thp_hands < spec.target_hands:
            issues.append(f"thp_incomplete_for_full_certification: hands={thp_hands} target={spec.target_hands}")
    elif thp_hands < spec.target_hands and not _log_target_reached(receipt, spec.target_hands):
        # Short smoke stops the official EXE before its natural 70-hand THP export.
        # Use bot/platform logs as smoke evidence; full certification still requires THP.
        issues.append(f"smoke_progress_incomplete: thp_hands={thp_hands} target={spec.target_hands}")
    return issues


def receipt_valid_for_spec(receipt: dict[str, Any], spec: CertificationSpec) -> bool:
    return not receipt_validation_issues(receipt, spec)


def report_validation_issues(report: dict[str, Any], spec: CertificationSpec) -> list[str]:
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
    for index, receipt in enumerate(rounds, start=1):
        receipt_issues = receipt_validation_issues(dict(receipt), spec)
        issues.extend(f"round_{index}: {issue}" for issue in receipt_issues)
    return issues


def report_valid_for_spec(report: dict[str, Any], spec: CertificationSpec) -> bool:
    return not report_validation_issues(report, spec)


def _cache_hit(spec: CertificationSpec, config: OfficialPlatformConfig | None = None) -> dict[str, Any] | None:
    key = cache_key(spec, config)
    payload = _read_json(_cache_file(key))
    if payload and payload.get("cache_key") == key and report_valid_for_spec(payload.get("result", {}), spec):
        return payload
    return None


def _write_cache(spec: CertificationSpec, result: dict[str, Any], config: OfficialPlatformConfig | None = None) -> str:
    key = cache_key(spec, config)
    payload = {
        "cache_key": key,
        "created_at": now_iso(),
        "spec": asdict(spec),
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
        "status": STATUS_LOCAL_PASS,
        "status_label": "local-pass",
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
    if current.get("status") in {STATUS_SMOKE_PASS, STATUS_COMPLIANCE_PASS, STATUS_PENDING, STATUS_CERTIFIED}:
        return current
    if current.get("status") == STATUS_FAILED and official_failure_blocks_parent(current):
        return current
    return write_status(candidate, STATUS_LOCAL_PASS, source=source, issues=[])


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
    inconclusive_issues = [
        issue for issue in issues
        if _issue_has_marker(issue, COMPLIANCE_INCONCLUSIVE_FAILURE_MARKERS)
    ]
    if violation_issues:
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


def parent_eligible(candidate: str | Path) -> bool:
    return not official_failure_blocks_parent(read_status(candidate))


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
    return CertificationSpec(
        mode=mode,
        candidate=str(data["candidate"]),
        opponent=str(data["opponent"]) if data.get("opponent") else None,
        self_play_rounds=int(data["self_play_rounds"]),
        opponent_rounds=int(data["opponent_rounds"]),
        target_hands=int(data["target_hands"]),
        round_timeout_sec=float(data["round_timeout_sec"]),
        no_progress_timeout_sec=float(data["no_progress_timeout_sec"]),
    )


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
    key = cache_key(spec, config)
    current = read_status(spec.candidate)
    if current.get("cache_key") == key and current.get("status") in {
        STATUS_PENDING,
        STATUS_SMOKE_PASS,
        STATUS_COMPLIANCE_PASS,
        STATUS_CERTIFIED,
        STATUS_INCONCLUSIVE,
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
        cache_key=key,
        reason=reason,
        queued=True,
        issues=[],
    )


def _status_for_result(spec: CertificationSpec, result: dict[str, Any], *, cache_hit: bool, cache_key_value: str) -> dict[str, Any]:
    validation_issues = report_validation_issues(result, spec)
    valid = not validation_issues
    issues = list(result.get("issues") or validation_issues)
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
    report = result.get("report", {}) if isinstance(result, dict) else {}
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    return write_status(
        spec.candidate,
        status,
        mode=spec.mode,
        cache_hit=cache_hit,
        cache_key=cache_key_value,
        summary=summary,
        issues=issues,
        result=result,
    )


def run_certification(
    spec: CertificationSpec,
    *,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    queue_on_busy: bool = True,
    runner: Runner = run_official_acceptance_sync,
) -> dict[str, Any]:
    cfg = config or OfficialPlatformConfig()
    cfg = _copy_config(
        cfg,
        round_timeout_sec=spec.round_timeout_sec,
        no_progress_timeout_sec=spec.no_progress_timeout_sec,
        results_dir=certification_root() / spec.mode,
    )
    key = cache_key(spec, cfg)
    if not force:
        cached = _cache_hit(spec, cfg)
        if cached:
            return _status_for_result(spec, cached["result"], cache_hit=True, cache_key_value=key)
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
    if report_valid_for_spec(result, spec):
        key = _write_cache(spec, result, cfg)
    return _status_for_result(spec, result, cache_hit=False, cache_key_value=key)


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
    payload["certification_root"] = str(certification_root())
    return payload
