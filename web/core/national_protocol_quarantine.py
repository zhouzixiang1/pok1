"""Content-bound quarantine and one-way bootstrap for national bot runtimes.

Published historical bots are useful strategy evidence, but their immutable
``national_bot.py`` launchers predate the current delimiter-free stream decoder
and bounded decision runtime.  They therefore cannot share an API with bots
that are safe to execute.  This module owns the explicit boundary:

* the repository policy binds every known legacy active artifact to its tag and
  completion tree and marks it non-executable;
* a *strict published bot* must pass the current stream-decoder and decision-
  runtime contract, in addition to having an immutable publication identity;
* before the first strict publication, exactly national_v142 may seed strategy
  files for one migration generation; it never enters an executable pool;
* with exactly one strict publication, the scheduler may create the second bot
  without waiting for an impossible one-bot rating sample.

Bootstrap receipts contain no timestamps, so they can be recomputed exactly at
every boundary.  Any policy, Git identity, pool, or source drift fails closed.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from bot_namespace import (
    bot_name,
    bot_tag,
    is_active_bot_name,
    parse_bot_version,
    version_sort_key,
)


ROOT = Path(__file__).resolve().parents[2]
BOTS_DIR = ROOT / "bots"
POLICY_PATH = ROOT / "web" / "core" / "national_protocol_quarantine.json"

POLICY_SCHEMA_VERSION = 1
POLICY_KIND = "national-native-protocol-quarantine"
BOOTSTRAP_RECEIPT_SCHEMA_VERSION = 1
BOOTSTRAP_RECEIPT_KIND = "national-native-protocol-bootstrap-source"
EXPECTED_QUARANTINED_VERSIONS = frozenset(
    {111, 112, 114, 119, 120, 121, 122, 123, 135, 141, 142}
)
MIGRATION_SEED_VERSION = 142
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_FIELDS = (
    "artifact_hash",
    "tag",
    "tag_object",
    "completion_tree_oid",
)
_HEALTH_CACHE_TTL_SEC = 10.0
_HEALTH_CACHE_LOCK = threading.RLock()
_HEALTH_REFRESH_LOCK = threading.Lock()
_HEALTH_CACHE: dict[str, Any] = {
    "key": None,
    "checked_at": 0.0,
    "report": None,
}
_ARTIFACT_CACHE_DIRECTORY_NAMES = frozenset(
    {"__pycache__", ".pytest_cache", ".task_context"}
)
_ARTIFACT_CACHE_FILE_NAMES = frozenset({".completed"})
_ARTIFACT_CACHE_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})


class ProtocolQuarantineError(RuntimeError):
    """Raised when the tracked quarantine authority is malformed."""


def _canonical_digest(payload: dict[str, Any]) -> str:
    from bot_artifact import canonical_digest

    return canonical_digest(payload)


def _published_identity(path: Path) -> dict[str, Any]:
    from bot_artifact import published_bot_identity

    return published_bot_identity(path)


def _strict_contract_errors(path: Path) -> list[str]:
    from national_native import check_native_contract

    return list(
        check_native_contract(
            path,
            require_current_stream_decoder=True,
            require_current_decision_runtime=True,
        )
    )


def _repository_ref_snapshot() -> tuple[str, ...]:
    """Read every ref that can change a quarantine publication in one command."""

    result = subprocess.run(
        ["git", "show-ref", "--head", "--dereference"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return (f"git_ref_snapshot_error:{result.returncode}", result.stderr[:200])
    wanted = {"HEAD", "refs/heads/main"} | {
        f"refs/tags/{bot_tag(version)}"
        for version in EXPECTED_QUARANTINED_VERSIONS
    }
    return tuple(
        line
        for line in result.stdout.splitlines()
        if line.rpartition(" ")[2].removesuffix("^{}") in wanted
    )


def _artifact_stat_snapshot(root: Path) -> tuple[tuple[Any, ...], ...]:
    """Cheaply invalidate cached content verification on any tree mutation."""

    rows: list[tuple[Any, ...]] = []
    for version in sorted(EXPECTED_QUARANTINED_VERSIONS):
        bot_dir = root / bot_name(version)
        try:
            root_stat = bot_dir.lstat()
        except OSError as exc:
            rows.append((bot_dir.name, "missing", type(exc).__name__))
            continue
        rows.append(
            (
                bot_dir.name,
                ".",
                root_stat.st_mode,
                root_stat.st_ino,
                root_stat.st_size,
                root_stat.st_mtime_ns,
                root_stat.st_ctime_ns,
            )
        )
        for directory, dirnames, filenames in os.walk(bot_dir, followlinks=False):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in _ARTIFACT_CACHE_DIRECTORY_NAMES
            )
            artifact_files = [
                name
                for name in filenames
                if name not in _ARTIFACT_CACHE_FILE_NAMES
                and Path(name).suffix.lower() not in _ARTIFACT_CACHE_FILE_SUFFIXES
            ]
            for name in sorted([*dirnames, *artifact_files]):
                path = Path(directory) / name
                try:
                    metadata = path.lstat()
                    relative = path.relative_to(bot_dir).as_posix()
                    rows.append(
                        (
                            bot_dir.name,
                            relative,
                            metadata.st_mode,
                            metadata.st_ino,
                            metadata.st_size,
                            metadata.st_mtime_ns,
                            metadata.st_ctime_ns,
                        )
                    )
                except OSError as exc:
                    rows.append((bot_dir.name, str(path), type(exc).__name__))
    return tuple(rows)


def _health_cache_key(policy_path: Path, bots_dir: Path) -> tuple[Any, ...]:
    try:
        policy_digest = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    except OSError as exc:
        policy_digest = f"unreadable:{type(exc).__name__}:{str(exc)[:120]}"
    return (
        policy_digest,
        _repository_ref_snapshot(),
        _artifact_stat_snapshot(bots_dir),
    )


def load_protocol_quarantine_policy(
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Load and structurally validate the repository-owned policy."""

    policy_path = Path(path) if path is not None else POLICY_PATH
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProtocolQuarantineError(
            f"protocol quarantine policy unreadable:{type(exc).__name__}"
        ) from exc
    if not isinstance(raw, dict):
        raise ProtocolQuarantineError("protocol quarantine policy must be an object")
    if raw.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ProtocolQuarantineError("protocol quarantine policy schema mismatch")
    if raw.get("kind") != POLICY_KIND:
        raise ProtocolQuarantineError("protocol quarantine policy kind mismatch")
    if not str(raw.get("policy_id") or "").strip():
        raise ProtocolQuarantineError("protocol quarantine policy_id missing")
    entries = raw.get("quarantined_artifacts")
    if not isinstance(entries, list):
        raise ProtocolQuarantineError("quarantined_artifacts must be a list")

    versions: set[int] = set()
    labels: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ProtocolQuarantineError("quarantine entry must be an object")
        try:
            version = int(item.get("version"))
        except (TypeError, ValueError) as exc:
            raise ProtocolQuarantineError("quarantine entry version invalid") from exc
        label = str(item.get("bot") or "")
        if version in versions or label in labels:
            raise ProtocolQuarantineError(f"duplicate quarantine entry:{label or version}")
        if label != bot_name(version) or parse_bot_version(label) != version:
            raise ProtocolQuarantineError(f"quarantine label/version mismatch:{label}")
        if str(item.get("tag") or "") != bot_tag(version):
            raise ProtocolQuarantineError(f"quarantine tag mismatch:{label}")
        if not _HEX64.fullmatch(str(item.get("artifact_hash") or "")):
            raise ProtocolQuarantineError(f"quarantine artifact_hash invalid:{label}")
        for field in ("tag_object", "completion_tree_oid"):
            if not _HEX40.fullmatch(str(item.get(field) or "")):
                raise ProtocolQuarantineError(f"quarantine {field} invalid:{label}")
        if item.get("disposition") not in {
            "historical_non_executable",
            "migration_strategy_seed_only",
        }:
            raise ProtocolQuarantineError(f"quarantine disposition invalid:{label}")
        versions.add(version)
        labels.add(label)

    if versions != EXPECTED_QUARANTINED_VERSIONS:
        raise ProtocolQuarantineError(
            "quarantine version set mismatch:"
            f"expected={sorted(EXPECTED_QUARANTINED_VERSIONS)}:actual={sorted(versions)}"
        )
    seed = raw.get("migration_seed")
    if not isinstance(seed, dict):
        raise ProtocolQuarantineError("migration_seed must be an object")
    if (
        seed.get("version") != MIGRATION_SEED_VERSION
        or seed.get("bot") != bot_name(MIGRATION_SEED_VERSION)
        or seed.get("closes_when") != "first_strict_published_bot_exists"
        or seed.get("required_prepare_action")
        != "replace_system_owned_national_bot_before_snapshot"
    ):
        raise ProtocolQuarantineError("migration_seed contract mismatch")
    seed_entry = next(
        item for item in entries if item["version"] == MIGRATION_SEED_VERSION
    )
    if seed_entry.get("disposition") != "migration_strategy_seed_only":
        raise ProtocolQuarantineError("migration seed disposition mismatch")
    return raw


def protocol_quarantine_health(
    *,
    policy_path: str | Path | None = None,
    bots_dir: str | Path | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Verify every tracked artifact against current worktree and Git identity."""

    issues: list[str] = []
    selected_policy_path = Path(policy_path) if policy_path is not None else POLICY_PATH
    root = Path(bots_dir) if bots_dir is not None else BOTS_DIR
    cacheable = policy_path is None and bots_dir is None
    cache_key = _health_cache_key(selected_policy_path, root) if cacheable else None
    if cacheable and not force_refresh:
        with _HEALTH_CACHE_LOCK:
            cached = _HEALTH_CACHE.get("report")
            fresh = time.monotonic() - float(_HEALTH_CACHE.get("checked_at") or 0.0)
            if (
                isinstance(cached, dict)
                and _HEALTH_CACHE.get("key") == cache_key
                and fresh <= _HEALTH_CACHE_TTL_SEC
            ):
                return deepcopy(cached)
    refresh_lock_acquired = False
    if cacheable:
        # A cold process may have daemon, API, and scheduler readers arrive at
        # once. Only one thread performs the expensive Git/artifact verification;
        # waiters re-check the freshly populated cache below.
        _HEALTH_REFRESH_LOCK.acquire()
        refresh_lock_acquired = True
        cache_key = _health_cache_key(selected_policy_path, root)
        if not force_refresh:
            with _HEALTH_CACHE_LOCK:
                cached = _HEALTH_CACHE.get("report")
                fresh = time.monotonic() - float(
                    _HEALTH_CACHE.get("checked_at") or 0.0
                )
                if (
                    isinstance(cached, dict)
                    and _HEALTH_CACHE.get("key") == cache_key
                    and fresh <= _HEALTH_CACHE_TTL_SEC
                ):
                    _HEALTH_REFRESH_LOCK.release()
                    return deepcopy(cached)
    try:
        policy = load_protocol_quarantine_policy(selected_policy_path)
    except Exception as exc:
        report = {
            "valid": False,
            "policy_id": "",
            "policy_digest": "",
            "quarantined_versions": [],
            "entries": {},
            "issues": [f"policy_invalid:{type(exc).__name__}:{str(exc)[:240]}"],
        }
        if cacheable:
            with _HEALTH_CACHE_LOCK:
                _HEALTH_CACHE.update(
                    key=cache_key,
                    checked_at=time.monotonic(),
                    report=deepcopy(report),
                )
        if refresh_lock_acquired:
            _HEALTH_REFRESH_LOCK.release()
        return report

    entries = {
        str(item["bot"]): dict(item)
        for item in policy["quarantined_artifacts"]
    }
    for label, expected in entries.items():
        path = root / label
        try:
            identity = _published_identity(path)
        except Exception as exc:
            issues.append(
                f"{label}:identity_error:{type(exc).__name__}:{str(exc)[:160]}"
            )
            continue
        if identity.get("published") is not True:
            detail = ",".join(str(item) for item in identity.get("issues", [])[:8])
            issues.append(f"{label}:not_published:{detail}")
        for field in _IDENTITY_FIELDS:
            actual = str(identity.get(field) or "")
            if actual != str(expected.get(field) or ""):
                issues.append(f"{label}:{field}_mismatch")

    report = {
        "valid": not issues,
        "policy_id": str(policy["policy_id"]),
        "policy_digest": _canonical_digest(policy),
        "quarantined_versions": sorted(EXPECTED_QUARANTINED_VERSIONS),
        "entries": entries,
        "issues": issues,
    }
    if cacheable:
        with _HEALTH_CACHE_LOCK:
            _HEALTH_CACHE.update(
                key=cache_key,
                checked_at=time.monotonic(),
                report=deepcopy(report),
            )
    if refresh_lock_acquired:
        _HEALTH_REFRESH_LOCK.release()
    return report


def is_quarantined_version(
    version: int,
    *,
    health: dict[str, Any] | None = None,
) -> bool:
    """Return true only under a valid policy that binds this exact version."""

    report = health if health is not None else protocol_quarantine_health()
    return bool(
        report.get("valid")
        and int(version) in set(report.get("quarantined_versions") or [])
    )


def quarantined_native_entry_sources(
    bot_dir: str | Path,
    *,
    health: dict[str, Any] | None = None,
    bots_dir: str | Path | None = None,
) -> tuple[str, ...]:
    """Return historical artifacts whose raw entry bytes match ``bot_dir``.

    Directory names are not an execution boundary: an obsolete
    ``national_bot.py`` can otherwise be copied into a differently named
    candidate and bypass a version-only quarantine.  The canonical historical
    bytes are trusted only after the complete publication policy verifies.
    Policy or source drift therefore fails closed before any bot is launched.
    """

    report = health if health is not None else protocol_quarantine_health()
    if report.get("valid") is not True:
        details = ";".join(
            str(item) for item in (report.get("issues") or [])[:6]
        )
        raise ProtocolQuarantineError(
            "protocol quarantine is not healthy for execution"
            + (f":{details}" if details else "")
        )

    entry = Path(bot_dir) / "national_bot.py"
    if not entry.is_file():
        return ()
    try:
        candidate_digest = hashlib.sha256(entry.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProtocolQuarantineError(
            f"native entry unreadable for quarantine check:{type(exc).__name__}"
        ) from exc

    root = Path(bots_dir) if bots_dir is not None else BOTS_DIR
    entries = report.get("entries")
    expected_labels = {
        bot_name(version) for version in EXPECTED_QUARANTINED_VERSIONS
    }
    if not isinstance(entries, dict) or set(entries) != expected_labels:
        raise ProtocolQuarantineError(
            "protocol quarantine health report is missing bound entries"
        )

    matches: list[str] = []
    for label in sorted(expected_labels, key=version_sort_key):
        historical_entry = root / label / "national_bot.py"
        try:
            historical_digest = hashlib.sha256(
                historical_entry.read_bytes()
            ).hexdigest()
        except OSError as exc:
            raise ProtocolQuarantineError(
                f"bound historical native entry unreadable:{label}:"
                f"{type(exc).__name__}"
            ) from exc
        if historical_digest == candidate_digest:
            matches.append(label)
    return tuple(matches)


def strict_published_bot_names(
    *,
    health: dict[str, Any] | None = None,
    bots_dir: str | Path | None = None,
) -> tuple[str, ...]:
    """Discover immutable bots satisfying the complete current runtime contract.

    The quarantine must itself be healthy before any executable publication is
    trusted.  Known quarantined identities remain excluded even if a future
    checker accidentally becomes more permissive.
    """

    report = health if health is not None else protocol_quarantine_health(
        bots_dir=bots_dir
    )
    if not report.get("valid"):
        return ()
    root = Path(bots_dir) if bots_dir is not None else BOTS_DIR
    quarantined = set(report.get("quarantined_versions") or [])
    strict: list[str] = []
    if not root.is_dir():
        return ()
    for path in root.iterdir():
        version = parse_bot_version(path.name)
        if (
            version is None
            or not is_active_bot_name(path.name)
            or version in quarantined
            or not path.is_dir()
        ):
            continue
        try:
            # Most historical directories fail inexpensive source-token checks.
            # Only run the much heavier Git publication resolver for artifacts
            # that already satisfy the complete executable contract.
            if _strict_contract_errors(path):
                continue
            identity = _published_identity(path)
            if identity.get("published") is not True:
                continue
        except Exception:
            continue
        strict.append(path.name)
    return tuple(sorted(strict, key=version_sort_key))


def _portable_source_identity(
    label: str,
    *,
    health: dict[str, Any],
    bots_dir: Path,
) -> dict[str, Any] | None:
    expected = (health.get("entries") or {}).get(label)
    if expected is not None:
        return {
            "bot": label,
            "version": int(expected["version"]),
            **{field: expected[field] for field in _IDENTITY_FIELDS},
        }
    try:
        identity = _published_identity(bots_dir / label)
    except Exception:
        return None
    if identity.get("published") is not True:
        return None
    version = parse_bot_version(label)
    if version is None:
        return None
    return {
        "bot": label,
        "version": int(version),
        **{field: str(identity.get(field) or "") for field in _IDENTITY_FIELDS},
    }


def select_protocol_bootstrap_source(
    active_bots: Iterable[str],
    *,
    policy_path: str | Path | None = None,
    bots_dir: str | Path | None = None,
    force_refresh: bool = True,
) -> dict[str, Any]:
    """Select the only legal no-rating source and return a durable receipt.

    ``active_bots`` is the already official/lifecycle-filtered executable pool.
    It must exactly agree with global strict publications for the zero/one-bot
    transition.  This avoids silently bootstrapping around an ineligible strict
    publication or using a quarantined artifact as a rating opponent.
    """

    root = Path(bots_dir) if bots_dir is not None else BOTS_DIR
    health = protocol_quarantine_health(
        policy_path=policy_path,
        bots_dir=root,
        force_refresh=force_refresh,
    )
    if not health.get("valid"):
        return {
            "available": False,
            "reason": "protocol_quarantine_policy_invalid",
            "issues": list(health.get("issues") or []),
        }
    active = tuple(sorted({str(item) for item in active_bots}, key=version_sort_key))
    strict = strict_published_bot_names(health=health, bots_dir=root)
    if active != strict:
        return {
            "available": False,
            "reason": "strict_publication_active_pool_mismatch",
            "active_bots": list(active),
            "strict_published_bots": list(strict),
        }
    if len(strict) >= 2:
        return {
            "available": False,
            "reason": "normal_strict_pool_ready",
            "strict_published_bots": list(strict),
        }
    if not strict:
        mode = "legacy_strategy_migration"
        source_label = bot_name(MIGRATION_SEED_VERSION)
    else:
        mode = "singleton_strict_pool"
        source_label = strict[0]
    source = _portable_source_identity(
        source_label,
        health=health,
        bots_dir=root,
    )
    if source is None:
        return {
            "available": False,
            "reason": "bootstrap_source_identity_unavailable",
            "source": source_label,
        }
    payload = {
        "schema_version": BOOTSTRAP_RECEIPT_SCHEMA_VERSION,
        "kind": BOOTSTRAP_RECEIPT_KIND,
        "mode": mode,
        "policy_id": health["policy_id"],
        "policy_digest": health["policy_digest"],
        "source": source,
        "strict_published_bots": list(strict),
        "rating_evidence": "intentionally_absent_until_two_strict_published_bots",
    }
    receipt = {**payload, "receipt_digest": _canonical_digest(payload)}
    return {
        "available": True,
        "reason": mode,
        "source_v": int(source["version"]),
        "source": source_label,
        "receipt": receipt,
    }


def validate_protocol_bootstrap_receipt(
    receipt: dict[str, Any] | None,
    *,
    active_bots: Iterable[str],
    policy_path: str | Path | None = None,
    bots_dir: str | Path | None = None,
) -> list[str]:
    """Recompute the complete transition receipt and reject any drift."""

    if not isinstance(receipt, dict):
        return ["protocol_bootstrap_receipt_missing"]
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    errors: list[str] = []
    if receipt.get("schema_version") != BOOTSTRAP_RECEIPT_SCHEMA_VERSION:
        errors.append("protocol_bootstrap_receipt_schema_mismatch")
    if receipt.get("kind") != BOOTSTRAP_RECEIPT_KIND:
        errors.append("protocol_bootstrap_receipt_kind_mismatch")
    if receipt.get("receipt_digest") != _canonical_digest(unsigned):
        errors.append("protocol_bootstrap_receipt_digest_mismatch")
    current = select_protocol_bootstrap_source(
        active_bots,
        policy_path=policy_path,
        bots_dir=bots_dir,
        force_refresh=True,
    )
    if not current.get("available"):
        errors.append(f"protocol_bootstrap_closed:{current.get('reason', 'unknown')}")
        errors.extend(str(item) for item in (current.get("issues") or [])[:8])
    elif current.get("receipt") != receipt:
        errors.append("protocol_bootstrap_receipt_current_state_mismatch")
    return errors


__all__ = [
    "EXPECTED_QUARANTINED_VERSIONS",
    "MIGRATION_SEED_VERSION",
    "ProtocolQuarantineError",
    "is_quarantined_version",
    "load_protocol_quarantine_policy",
    "protocol_quarantine_health",
    "quarantined_native_entry_sources",
    "select_protocol_bootstrap_source",
    "strict_published_bot_names",
    "validate_protocol_bootstrap_receipt",
]
