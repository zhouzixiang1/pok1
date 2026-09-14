"""Generation-scoped evidence snapshots for LLM planning/audit.

The rating daemon keeps updating live result files while Master and audit LLMs
run. A plan that cites live H2H counts or reopens replay files can become stale
minutes later even if those bytes were correct when planning began. This module
creates one stable per-generation snapshot for strength rows, action evidence,
match-history cutoffs, and deterministic replay spotlight citations so every
planning/audit stage validates the same content-addressed contract.
"""

from __future__ import annotations

from contextlib import contextmanager
import bisect
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from strength_order import match_score
from bot_namespace import ACTIVE_BOT_PREFIX, bot_name


SNAPSHOT_DIRNAME = "evidence_snapshot"
H2H_SNAPSHOT_FILENAME = "head_to_head.json"
BOT_STATS_SNAPSHOT_FILENAME = "bot_stats.json"
RATINGS_SNAPSHOT_FILENAME = "glicko_ratings.json"
SELECTION_SNAPSHOT_FILENAME = "selection_snapshot.json"
ACTION_STATS_SNAPSHOT_FILENAME = "bot_action_stats.json"
ACTION_STATS_PER_OPP_SNAPSHOT_FILENAME = "bot_action_stats_per_opp.json"
ACTION_STATS_SOURCE_FILENAME = "bot_action_stats_source.json"
MATCH_HISTORY_INDEX_FILENAME = "match_history_index.json"
REPLAY_SPOTLIGHT_FILENAME = "replay_spotlight.json"
MANIFEST_FILENAME = "manifest.json"
SNAPSHOT_SCHEMA_VERSION = 9
SNAPSHOT_FILES = {
    "h2h": H2H_SNAPSHOT_FILENAME,
    "bot_stats": BOT_STATS_SNAPSHOT_FILENAME,
    "ratings": RATINGS_SNAPSHOT_FILENAME,
    "selection": SELECTION_SNAPSHOT_FILENAME,
    "action_stats": ACTION_STATS_SNAPSHOT_FILENAME,
    "action_stats_per_opp": ACTION_STATS_PER_OPP_SNAPSHOT_FILENAME,
    "action_stats_source": ACTION_STATS_SOURCE_FILENAME,
    "match_history_index": MATCH_HISTORY_INDEX_FILENAME,
    "replay_spotlight": REPLAY_SPOTLIGHT_FILENAME,
}


def _infra():
    import evolution_infra

    return evolution_infra


def _snapshot_dir(next_v: int | str) -> Path:
    infra = _infra()
    return infra.RESULTS_DIR / f"v{int(next_v)}" / SNAPSHOT_DIRNAME


def _repo_rel(path: Path) -> str:
    infra = _infra()
    try:
        return str(path.resolve().relative_to(infra.PROJECT_ROOT.resolve())).replace("\\", "/")
    except Exception:
        return str(path)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _canonical_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _evaluation_identity_digest(results_dir: Path) -> str | None:
    from evaluation_bundle import validated_evaluation_identity_digest

    return validated_evaluation_identity_digest(results_dir)


@contextmanager
def _snapshot_lock(next_v: int | str) -> Iterator[None]:
    parent = _snapshot_dir(next_v).parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(parent / ".evidence_snapshot.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _snapshot_paths(next_v: int | str) -> tuple[Path, Path, Path]:
    directory = _snapshot_dir(next_v)
    return directory, directory / H2H_SNAPSHOT_FILENAME, directory / MANIFEST_FILENAME


def _snapshot_payload_paths(next_v: int | str) -> dict[str, Path]:
    directory = _snapshot_dir(next_v)
    return {role: directory / filename for role, filename in SNAPSHOT_FILES.items()}


def _payload_entries(role: str, parsed: dict[str, Any]) -> int:
    if role in {"selection", "match_history_index", "replay_spotlight"}:
        rows = parsed.get("rows")
        if role == "match_history_index":
            rows = parsed.get("entries")
        elif role == "replay_spotlight":
            rows = parsed.get("citations")
        return len(rows) if isinstance(rows, list) else -1
    return len(parsed)


def _validate_existing_snapshot(next_v: int | str) -> tuple[dict[str, Any] | None, list[str]]:
    directory, snapshot_path, manifest_path = _snapshot_paths(next_v)
    payload_paths = _snapshot_payload_paths(next_v)
    issues: list[str] = []
    if not directory.is_dir() or directory.is_symlink():
        return None, ["snapshot_directory_missing_or_unsafe"]
    for role, payload_path in payload_paths.items():
        if not payload_path.is_file() or payload_path.is_symlink():
            issues.append(f"snapshot_{role}_missing_or_unsafe")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        issues.append("snapshot_manifest_missing_or_unsafe")
    manifest = _read_manifest(manifest_path)
    if manifest is None:
        issues.append("snapshot_manifest_invalid_json")
        return None, issues
    if manifest.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        issues.append("snapshot_schema_mismatch")
    if manifest.get("next_v") != int(next_v):
        issues.append("snapshot_version_mismatch")
    claimed_digest = str(manifest.get("manifest_digest") or "")
    actual_digest = _canonical_digest({
        key: value for key, value in manifest.items() if key != "manifest_digest"
    })
    if claimed_digest != actual_digest:
        issues.append("snapshot_manifest_digest_mismatch")
    file_contracts = manifest.get("files")
    if not isinstance(file_contracts, dict):
        issues.append("snapshot_file_contracts_missing")
        file_contracts = {}
    if issues:
        return None, issues
    for role, payload_path in payload_paths.items():
        contract = file_contracts.get(role)
        if not isinstance(contract, dict):
            issues.append(f"snapshot_{role}_contract_missing")
            continue
        if contract.get("filename") != SNAPSHOT_FILES[role]:
            issues.append(f"snapshot_{role}_filename_mismatch")
        try:
            payload = payload_path.read_bytes()
            parsed = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            issues.append(f"snapshot_{role}_invalid:{type(exc).__name__}")
            continue
        if not isinstance(parsed, dict):
            issues.append(f"snapshot_{role}_not_object")
            continue
        if contract.get("sha256") != _sha256(payload):
            issues.append(f"snapshot_{role}_digest_mismatch")
        if int(contract.get("bytes", -1)) != len(payload):
            issues.append(f"snapshot_{role}_size_mismatch")
        if int(contract.get("entries", -1)) != _payload_entries(role, parsed):
            issues.append(f"snapshot_{role}_entry_count_mismatch")
    h2h_contract = file_contracts.get("h2h") or {}
    if manifest.get("sha256") != h2h_contract.get("sha256"):
        issues.append("snapshot_h2h_alias_digest_mismatch")
    if manifest.get("bytes") != h2h_contract.get("bytes"):
        issues.append("snapshot_h2h_alias_size_mismatch")
    if manifest.get("entries") != h2h_contract.get("entries"):
        issues.append("snapshot_h2h_alias_entry_count_mismatch")
    current_identity = _evaluation_identity_digest(_infra().RESULTS_DIR)
    if current_identity is None:
        issues.append("snapshot_evaluation_identity_invalid")
    elif manifest.get("evaluation_identity_digest") != current_identity:
        issues.append("snapshot_evaluation_identity_mismatch")
    return (manifest if not issues else None), issues


def _write_file_durable(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def ensure_generation_h2h_snapshot(
    next_v: int | str,
    *,
    force: bool = False,
    spotlight_bot: str | None = None,
) -> dict[str, Any]:
    """Create or return the stable, same-cycle evaluation snapshot for ``next_v``."""
    infra = _infra()
    snapshot_dir, snapshot_path, manifest_path = _snapshot_paths(next_v)
    payload_paths = _snapshot_payload_paths(next_v)

    with _snapshot_lock(next_v):
        if snapshot_dir.exists() and not force:
            manifest, issues = _validate_existing_snapshot(next_v)
            if (
                manifest is not None
                and spotlight_bot is not None
                and manifest.get("spotlight_bot") != spotlight_bot
            ):
                issues.append("snapshot_spotlight_bot_mismatch")
                manifest = None
            if manifest is None:
                return {
                    "available": False,
                    "reason": "snapshot_integrity_failure",
                    "issues": issues,
                    "h2h_path": str(snapshot_path),
                    "h2h_relpath": _repo_rel(snapshot_path),
                    "manifest_path": str(manifest_path),
                    "manifest_relpath": _repo_rel(manifest_path),
                    "reused": True,
                }
            return {
                **manifest,
                "available": True,
                "h2h_path": str(snapshot_path),
                "h2h_relpath": _repo_rel(snapshot_path),
                "manifest_path": str(manifest_path),
                "manifest_relpath": _repo_rel(manifest_path),
                "reused": True,
            }
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)

        from evaluation_bundle import load_published_evaluation_bundle

        bundle = load_published_evaluation_bundle(infra.RESULTS_DIR)
        if not bundle.get("available"):
            return {
                "available": False,
                "reason": bundle.get("reason", "cycle_bundle_unavailable"),
                "issues": bundle.get("issues", []),
                "h2h_path": str(snapshot_path),
                "h2h_relpath": _repo_rel(snapshot_path),
                "reused": False,
            }
        raw_files = {
            role: payload
            for role, payload in bundle["raw_files"].items()
            if role in SNAPSHOT_FILES
        }
        parsed_files = {
            "h2h": bundle["h2h"],
            "bot_stats": bundle["bot_stats"],
            "ratings": bundle["ratings"],
            "selection": bundle["selection"],
        }
        cycle_manifest = bundle["manifest"]
        # Action frequencies are advisory rather than rating authority, but
        # Master retries still need one stable view.  The async writer uses the
        # same cycle lock around this pair, so they are captured from one scan.
        from evaluation_bundle import evaluation_cycle_lock

        with evaluation_cycle_lock(infra.RESULTS_DIR, exclusive=False):
            for role, filename in (
                ("action_stats", ACTION_STATS_SNAPSHOT_FILENAME),
                ("action_stats_per_opp", ACTION_STATS_PER_OPP_SNAPSHOT_FILENAME),
                ("action_stats_source", ACTION_STATS_SOURCE_FILENAME),
            ):
                value = infra.read_locked_json(infra.RESULTS_DIR / filename, default={})
                if not isinstance(value, dict):
                    value = {}
                payload = json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")
                raw_files[role] = payload
                parsed_files[role] = value
        action_stats_source = parsed_files.get("action_stats_source") or {}
        from bot_action_stats import MAX_ACTION_STATS_CYCLE_LAG

        try:
            action_stats_source_save = int(
                action_stats_source.get("source_cycle_save_num", -1)
            )
            snapshot_cycle_save = int(cycle_manifest.get("save_num", -1))
        except (TypeError, ValueError):
            action_stats_source_save = -1
            snapshot_cycle_save = -1
        action_stats_cycle_lag = snapshot_cycle_save - action_stats_source_save
        if (
            action_stats_source.get("evaluation_identity_digest")
            != str(cycle_manifest.get("evaluation_identity_digest") or "")
            or not str(action_stats_source.get("source_cycle_manifest_digest") or "")
            or action_stats_source_save < 0
            or action_stats_cycle_lag < 0
            or action_stats_cycle_lag > MAX_ACTION_STATS_CYCLE_LAG
        ):
            for role in ("action_stats", "action_stats_per_opp"):
                parsed_files[role] = {}
                raw_files[role] = b"{}"
            parsed_files["action_stats_source"] = {
                "available": False,
                "reason": "no_bounded_same_identity_committed_action_scan",
            }
            raw_files["action_stats_source"] = json.dumps(
                parsed_files["action_stats_source"],
                indent=2,
            ).encode("utf-8")
        else:
            parsed_files["action_stats_source"] = {
                **action_stats_source,
                "snapshot_cycle_save_num": snapshot_cycle_save,
                "snapshot_cycle_lag": action_stats_cycle_lag,
                "bounded_stale": action_stats_cycle_lag > 0,
            }
            raw_files["action_stats_source"] = json.dumps(
                parsed_files["action_stats_source"],
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8")
        active_set = set(cycle_manifest.get("active_bots") or [])
        cycle_identity = str(
            cycle_manifest.get("evaluation_identity_digest") or ""
        )
        history_entries = []
        from rating_snapshot import _admitted_70_hand_history_sample

        for line in bundle["raw_append_logs"]["match_history"].splitlines():
            try:
                row = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("evaluation_identity_digest") != cycle_identity:
                continue
            if row.get("bot0") not in active_set or row.get("bot1") not in active_set:
                continue
            if _admitted_70_hand_history_sample(
                row,
                expected_evaluation_identity_digest=cycle_identity,
                replay_dir=infra.RESULTS_DIR / "match_replay",
            ) is None:
                continue
            history_entries.append(row)
        history_entries = history_entries[-512:]
        history_index = {
            "schema_version": 1,
            "evaluation_identity_digest": cycle_identity,
            "cycle_manifest_digest": bundle["manifest_digest"],
            "active_bots": sorted(active_set),
            "entries": history_entries,
            "replay_ids": [str(row.get("id")) for row in history_entries if row.get("id")],
        }
        history_payload = json.dumps(
            history_index,
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8")
        raw_files["match_history_index"] = history_payload
        parsed_files["match_history_index"] = history_index
        from replay_spotlight import build_critical_hands_evidence

        if spotlight_bot:
            with evaluation_cycle_lock(infra.RESULTS_DIR, exclusive=False):
                replay_spotlight = build_critical_hands_evidence(
                    spotlight_bot,
                    infra.RESULTS_DIR / "match_replay",
                    max_hands=10,
                    recent_n_files=20,
                    allowed_replay_ids=history_index["replay_ids"],
                    expected_evaluation_identity_digest=cycle_identity,
                )
        else:
            replay_spotlight = {
                "schema_version": 2,
                "epoch": "national_tcp_policy_v1",
                "execution_mode": "native_tcp",
                "evaluation_identity_digest": cycle_identity,
                "bot": "",
                "text": "",
                "citations": [],
                "source_replays": {},
            }
        replay_spotlight_payload = json.dumps(
            replay_spotlight,
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8")
        raw_files["replay_spotlight"] = replay_spotlight_payload
        parsed_files["replay_spotlight"] = replay_spotlight
        snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary_dir = Path(tempfile.mkdtemp(
            prefix=f".{SNAPSHOT_DIRNAME}-",
            dir=snapshot_dir.parent,
        ))
        try:
            temporary_manifest = temporary_dir / MANIFEST_FILENAME
            file_contracts = {}
            for role, filename in SNAPSHOT_FILES.items():
                payload = raw_files[role]
                _write_file_durable(temporary_dir / filename, payload)
                file_contracts[role] = {
                    "filename": filename,
                    "sha256": _sha256(payload),
                    "bytes": len(payload),
                    "entries": _payload_entries(role, parsed_files[role]),
                }
            h2h_contract = file_contracts["h2h"]
            manifest = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "available": True,
                "next_v": int(next_v),
                "created_at": time.time(),
                "h2h_relpath": _repo_rel(snapshot_path),
                "bot_stats_relpath": _repo_rel(payload_paths["bot_stats"]),
                "ratings_relpath": _repo_rel(payload_paths["ratings"]),
                "selection_relpath": _repo_rel(payload_paths["selection"]),
                "action_stats_relpath": _repo_rel(payload_paths["action_stats"]),
                "action_stats_per_opp_relpath": _repo_rel(
                    payload_paths["action_stats_per_opp"]
                ),
                "match_history_index_relpath": _repo_rel(
                    payload_paths["match_history_index"]
                ),
                "replay_spotlight_relpath": _repo_rel(
                    payload_paths["replay_spotlight"]
                ),
                "manifest_relpath": _repo_rel(manifest_path),
                "spotlight_bot": spotlight_bot or "",
                # Bind the identity that the cycle manifest proved while the
                # shared cycle lock was held. Re-reading the current identity
                # here would allow a concurrent migration to relabel old bytes
                # as belonging to the new evaluator.
                "evaluation_identity_digest": str(
                    cycle_manifest.get("evaluation_identity_digest") or "missing"
                ),
                "cycle": {
                    "manifest_digest": bundle["manifest_digest"],
                    "save_num": int(cycle_manifest.get("save_num", -1)),
                    "daemon_run_id": str(cycle_manifest.get("daemon_run_id") or ""),
                    "active_bots": list(cycle_manifest.get("active_bots") or []),
                },
                "files": file_contracts,
                # Backward-compatible aliases: these always describe H2H.
                "sha256": h2h_contract["sha256"],
                "bytes": h2h_contract["bytes"],
                "entries": h2h_contract["entries"],
            }
            manifest["manifest_digest"] = _canonical_digest(manifest)
            _write_file_durable(
                temporary_manifest,
                json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"),
            )
            directory_fd = os.open(temporary_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.rename(temporary_dir, snapshot_dir)
            parent_fd = os.open(snapshot_dir.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir, ignore_errors=True)

    try:
        from system_log import log_system_event

        log_system_event(
            "pipeline.h2h_snapshot_created",
            "info",
            f"H2H evidence snapshot created for v{int(next_v)}",
            {k: manifest[k] for k in ("next_v", "h2h_relpath", "sha256", "entries", "bytes", "manifest_digest")},
        )
    except Exception:
        pass
    return {
        **manifest,
        "available": True,
        "reused": False,
        "h2h_path": str(snapshot_path),
        "bot_stats_path": str(payload_paths["bot_stats"]),
        "ratings_path": str(payload_paths["ratings"]),
        "selection_path": str(payload_paths["selection"]),
        "action_stats_path": str(payload_paths["action_stats"]),
        "action_stats_per_opp_path": str(payload_paths["action_stats_per_opp"]),
        "match_history_index_path": str(payload_paths["match_history_index"]),
        "replay_spotlight_path": str(payload_paths["replay_spotlight"]),
        "manifest_path": str(manifest_path),
    }


def load_generation_evaluation_snapshot(next_v: int | str) -> dict[str, Any]:
    """Strictly load an existing immutable generation evaluation bundle.

    This read API never creates a snapshot.  Only prepare_generation owns the
    cutoff-creation operation; every later stage must fail closed if its exact
    snapshot was removed, migrated, or replaced.
    """
    from evaluation_bundle import evaluation_cycle_lock

    with _snapshot_lock(next_v), evaluation_cycle_lock(
        _infra().RESULTS_DIR,
        exclusive=False,
    ):
        manifest, issues = _validate_existing_snapshot(next_v)
        if manifest is None:
            return {
                "available": False,
                "reason": "snapshot_integrity_failure",
                "issues": issues,
            }
        parsed = {}
        try:
            for role, path in _snapshot_payload_paths(next_v).items():
                parsed[role] = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "available": False,
                "reason": f"snapshot_read_failed:{type(exc).__name__}",
            }
    return {"available": True, "manifest": manifest, **parsed}


def load_generation_snapshot_identity(next_v: int | str) -> dict[str, Any]:
    """Return strict snapshot metadata without creating a new cutoff."""
    bundle = load_generation_evaluation_snapshot(next_v)
    if not bundle.get("available"):
        return bundle
    manifest = bundle["manifest"]
    directory, h2h_path, manifest_path = _snapshot_paths(next_v)
    payload_paths = _snapshot_payload_paths(next_v)
    return {
        **manifest,
        "available": True,
        "h2h_path": str(h2h_path),
        "h2h_relpath": _repo_rel(h2h_path),
        "bot_stats_path": str(payload_paths["bot_stats"]),
        "ratings_path": str(payload_paths["ratings"]),
        "selection_path": str(payload_paths["selection"]),
        "action_stats_path": str(payload_paths["action_stats"]),
        "action_stats_per_opp_path": str(payload_paths["action_stats_per_opp"]),
        "match_history_index_path": str(payload_paths["match_history_index"]),
        "manifest_path": str(manifest_path),
        "manifest_relpath": _repo_rel(manifest_path),
        "reused": True,
    }


def load_generation_h2h_snapshot(next_v: int | str) -> dict[str, Any]:
    bundle = load_generation_evaluation_snapshot(next_v)
    return bundle.get("h2h", {}) if bundle.get("available") else {}


def _row_versions(key: str) -> tuple[str | None, str | None]:
    match = _h2h_key_re().search(str(key or ""))
    if not match:
        return None, None
    return match.group(1), match.group(2)


def _row_win_rate(row: dict[str, Any]) -> float | None:
    try:
        games = int(row.get("games", 0) or 0)
        if games > 0 and (
            row.get("a_wins") is not None or row.get("draws") is not None
        ):
            return match_score(
                row.get("a_wins", 0),
                row.get("draws", 0),
                games,
            )
        if row.get("win_rate") is not None:
            return float(row.get("win_rate"))
        return None
    except Exception:
        return None


def build_h2h_prompt_summary(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    max_rows: int = 64,
    confirmed_games: int = 10,
) -> str:
    """Return a compact, citation-safe H2H summary for prompts.

    The full snapshot remains the source of truth. This summary gives Master and
    audit roles the exact row keys/counts they need most often without forcing a
    long live-file read or encouraging sparse-sample overclaims.
    """
    h2h = load_generation_h2h_snapshot(next_v)
    if not h2h:
        return "Compact H2H summary unavailable; stable snapshot has no rows."

    source = str(source_v) if source_v is not None else None
    rows: list[dict[str, Any]] = []
    for key, row in h2h.items():
        if not isinstance(row, dict):
            continue
        a_v, b_v = _row_versions(str(key))
        games = int(row.get("games", 0) or 0)
        a_wins = int(row.get("a_wins", 0) or 0)
        b_wins = int(row.get("b_wins", 0) or 0)
        draws = int(row.get("draws", 0) or 0)
        wr_a = _row_win_rate(row)
        if wr_a is None:
            continue
        perspective = None
        source_wr = None
        source_wins = None
        source_losses = None
        if source and a_v == source:
            perspective = f"v{source}"
            source_wr = wr_a
            source_wins = a_wins
            source_losses = b_wins
        elif source and b_v == source:
            perspective = f"v{source}"
            source_wr = 1.0 - wr_a
            source_wins = b_wins
            source_losses = a_wins

        sample_class = "sparse"
        if games >= confirmed_games:
            if source_wr is not None and source_wr < 0.40:
                sample_class = "confirmed_weakness"
            elif source_wr is not None and source_wr > 0.60:
                sample_class = "confirmed_strength"
            else:
                sample_class = "adequate_context"

        rows.append({
            "key": str(key),
            "games": games,
            "a_wins": a_wins,
            "b_wins": b_wins,
            "draws": draws,
            "win_rate": wr_a,
            "source_match": perspective is not None,
            "source_wr": source_wr,
            "source_wins": source_wins,
            "source_losses": source_losses,
            "sample_class": sample_class,
            "canonical_citation": (
                f"{key}: games={games}, a_wins={a_wins}, "
                f"b_wins={b_wins}, draws={draws}, win_rate={wr_a:.4f}"
            ),
        })

    if source:
        rows.sort(key=lambda r: (
            not r["source_match"],
            {
                "confirmed_weakness": 0,
                "adequate_context": 1,
                "confirmed_strength": 2,
                "sparse": 3,
            }.get(r["sample_class"], 4),
            r["source_wr"] if r["source_wr"] is not None else 0.5,
            -r["games"],
        ))
    else:
        rows.sort(key=lambda r: -r["games"])
    if source:
        source_rows = [r for r in rows if r["source_match"]]
        other_rows = [r for r in rows if not r["source_match"]]
        rows = source_rows + other_rows[:max(0, max_rows - len(source_rows))]
    rows = rows[:max_rows]

    lines = [
        "Compact source-focused H2H summary from the stable snapshot:",
        f"- Adequate/confirmed matchup claims require games >= {confirmed_games}; otherwise label sparse/advisory.",
        f"- Statistical evidence bar (load-bearing weakness claims): cite one "
        f"matchup row with games >= 30 as the primary basis AND one aggregate "
        f"row with games >= 200 as corroboration — aggregate rows live in "
        f"bot_stats.json (per-bot games ~400+) and selection_snapshot.json. "
        f"Claims citing only n<30 rows are rejected as noise fitting.",
        "- Quote row key, games, a_wins, b_wins, draws, and win_rate exactly when citing a matchup.",
        "- Prefer the canonical_citation text below; do not derive matchup records from live H2H or match_history.",
    ]
    for r in rows:
        base = (
            f"- {r['key']}: games={r['games']}, a_wins={r['a_wins']}, "
            f"b_wins={r['b_wins']}, draws={r['draws']}, win_rate={r['win_rate']:.4f}, "
            f"class={r['sample_class']}"
        )
        if r["source_match"]:
            base += (
                f", source_wr={r['source_wr']:.4f}, "
                f"source_record={r['source_wins']}W/{r['source_losses']}L"
            )
        base += f", canonical_citation=\"{r['canonical_citation']}\""
        lines.append(base)
    return "\n".join(lines)


def _h2h_repair_row_line(key: str, row: dict, *, source_v: int | str | None = None) -> str:
    """One canonical-citation row line (single format source).

    Shared by the audit repair guidance (``h2h_citation_repair_guidance``)
    and the pre-plan exact-citable-rows table
    (``exact_citable_rows_preinjection``) so both always print the same
    ``canonical_citation`` bytes the audit compares against.
    """
    games = int(row.get("games", 0) or 0)
    a_wins = int(row.get("a_wins", 0) or 0)
    b_wins = int(row.get("b_wins", 0) or 0)
    draws = int(row.get("draws", 0) or 0)
    win_rate = _row_win_rate(row)
    if win_rate is None:
        win_rate = 0.0
    line = (
        f"- canonical_citation: {key}: games={games}, "
        f"a_wins={a_wins}, b_wins={b_wins}, draws={draws}, win_rate={win_rate:.4f}"
    )
    a_v, b_v = _row_versions(key)
    if source_v is not None and str(source_v) in {a_v, b_v}:
        source = str(source_v)
        if a_v == source:
            source_wins, source_losses, source_wr = a_wins, b_wins, win_rate
        else:
            source_wins, source_losses, source_wr = b_wins, a_wins, 1.0 - win_rate
        line += f" (v{source} perspective: {source_wins}W/{source_losses}L, wr={source_wr:.4f})"
    return line


def h2h_citation_repair_guidance(
    next_v: int | str,
    citation_errors: list[str],
    *,
    source_v: int | str | None = None,
    max_rows: int = 12,
    max_aggregate_refs: int = 6,
) -> str:
    """Return concrete snapshot rows to repair rejected H2H citations.

    Audit rejection feedback is often too negative ("the numbers are wrong")
    without giving the Master a replacement fact. This helper maps citation
    errors back to exact snapshot rows so the retry prompt contains the row key
    and counts to use verbatim.  When a rejection carries the two-tier token
    ``proposal_cited_sample_too_small`` (the aggregate corroboration leg),
    the guidance additionally lists the aggregate pointers that satisfy the
    CURRENT pool-annealed aggregate tier, with the tier number spelled out.
    """
    aggregate_errors = [
        str(err)
        for err in (citation_errors or [])
        if "proposal_cited_sample_too_small" in str(err)
    ]
    h2h = load_generation_h2h_snapshot(next_v)
    if (not h2h or not citation_errors) and not aggregate_errors:
        return ""

    wanted: list[str] = []
    seen: set[str] = set()
    for err in citation_errors:
        for key in re.findall(r"\(key ([^)]+)\)", str(err)):
            if key in h2h and key not in seen:
                wanted.append(key)
                seen.add(key)
        for alias_match in _h2h_key_re().finditer(str(err)):
            a_v, b_v = alias_match.group(1), alias_match.group(2)
            for key in (
                f"{bot_name(int(a_v))} vs {bot_name(int(b_v))}",
                f"{bot_name(int(b_v))} vs {bot_name(int(a_v))}",
            ):
                if key in h2h and key not in seen:
                    wanted.append(key)
                    seen.add(key)
        # Rejection tokens (2026-09-13) carry the charset-safe underscore
        # matchup form ``a_vs_b``; map it back onto both snapshot row keys
        # the same way the spaced alias above is mapped.
        for token_match in re.findall(
            rf"\b{re.escape(ACTIVE_BOT_PREFIX)}(\d+)_vs_"
            rf"{re.escape(ACTIVE_BOT_PREFIX)}(\d+)\b",
            str(err),
        ):
            a_v, b_v = token_match
            for key in (
                f"{bot_name(int(a_v))} vs {bot_name(int(b_v))}",
                f"{bot_name(int(b_v))} vs {bot_name(int(a_v))}",
            ):
                if key in h2h and key not in seen:
                    wanted.append(key)
                    seen.add(key)

    rows: list[str] = []
    for key in wanted[:max_rows]:
        row = h2h.get(key)
        if not isinstance(row, dict):
            continue
        rows.append(_h2h_repair_row_line(key, row, source_v=source_v))

    blocks: list[str] = []
    if rows:
        blocks.append("\n".join([
            "Use these exact stable snapshot rows to repair the rejected H2H citations:",
            *rows,
            "Do not replace them with live H2H, match_history, replay-window, or daemon-updated counts.",
        ]))
    if aggregate_errors:
        try:
            tier, pointers = _aggregate_tier_pointers(
                next_v, max_refs=max_aggregate_refs
            )
        except Exception:
            tier, pointers = 0, []
        if pointers:
            blocks.append("\n".join([
                "Aggregate corroboration leg: cite at least one of these exact "
                f"aggregate pointers whose bound games reach the current aggregate "
                f"tier (games >= {tier}):",
                *(
                    f"- {pointer} — games={games}"
                    for pointer, games in pointers
                ),
            ]))
    if not blocks:
        return ""
    return "\n\n".join(blocks)


def exact_citable_rows_preinjection(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    max_h2h_rows: int = 12,
    max_aggregate_refs: int = 6,
    max_chars: int = 2500,
) -> str:
    """Deterministic exact-citable-rows table injected into Master prompts.

    Two consecutive generations died at the plan audit because the final
    Master plan cited H2H games/wins numbers that disagreed with the frozen
    snapshot (hallucinated recall), and because no aggregate pointer was
    known to clear the annealed aggregate tier.  This table is rendered from
    the SAME frozen generation evidence snapshot the audit validates against
    (never live results), so the model can copy numbers instead of recalling
    them: the source parent's H2H rows (both directions, games desc, top 12)
    plus the aggregate pointers that satisfy the CURRENT pool-annealed
    aggregate tier (top 6, shared tier math with the audit mirror).

    Advisory and fail-open: an unreadable/missing snapshot yields ``""`` and
    the caller renders the prompt without this section — the gate remains the
    audit.  The section is hard-bounded by ``max_chars`` (trailing H2H rows
    are dropped first, never the aggregate pointers or the closing
    instruction).
    """
    try:
        bundle = load_generation_evaluation_snapshot(next_v)
        if not isinstance(bundle, dict) or not bundle.get("available"):
            return ""
        h2h = bundle.get("h2h") if isinstance(bundle.get("h2h"), dict) else {}

        h2h_entries: list[tuple[int, str, dict]] = []
        for key, row in h2h.items():
            if not isinstance(row, dict):
                continue
            a_v, b_v = _row_versions(str(key))
            if (
                source_v is not None
                and str(int(source_v)) not in {a_v, b_v}
            ):
                continue
            games = int(row.get("games", 0) or 0)
            if games <= 0:
                continue
            h2h_entries.append((games, str(key), row))
        h2h_entries.sort(key=lambda item: (-item[0], item[1]))
        h2h_lines = [
            _h2h_repair_row_line(key, row, source_v=source_v)
            for _games, key, row in h2h_entries[:max_h2h_rows]
        ]

        aggregate_tier, pointers = _aggregate_tier_pointers(
            next_v, bundle=bundle, max_refs=max_aggregate_refs
        )
        if pointers:
            aggregate_lines = [
                f"- {pointer} — games={games}"
                for pointer, games in pointers
            ]
            aggregate_note = (
                "Aggregate corroboration rows/pointers that satisfy the current "
                f"aggregate evidence tier (games >= {aggregate_tier}) — the plan "
                "must cite at least one of these as corroboration:"
            )
        else:
            aggregate_lines = []
            aggregate_note = (
                "No aggregate row currently reaches the aggregate evidence tier "
                f"(games >= {aggregate_tier}); do not fabricate aggregate counts."
            )

        def _render(h2h_block: list[str], aggregate_block: list[str]) -> str:
            source_label = (
                f"for source parent {bot_name(int(source_v))} "
                if source_v is not None
                else ""
            )
            return "\n".join([
                "EXACT CITABLE SNAPSHOT ROWS (system-rendered from the frozen "
                "generation evidence snapshot; the audit validates against this "
                "same snapshot):",
                f"Matchup H2H rows {source_label}(both directions, games "
                "descending, at most "
                f"{max_h2h_rows}) — when citing a matchup, copy one of these "
                "rows verbatim:",
                *h2h_block,
                aggregate_note,
                *aggregate_block,
                "Hard requirement: cite games/wins/draws numbers ONLY as printed "
                "above. The audit re-validates every cited number verbatim "
                "against this same frozen snapshot; recalled, recomputed, or "
                "live-file numbers fail the audit.",
            ])

        h2h_block = list(h2h_lines)
        aggregate_block = list(aggregate_lines)
        text = _render(h2h_block, aggregate_block)
        # Deterministic bound: drop trailing (weakest) H2H rows first; the
        # aggregate pointers and the closing instruction always survive.
        while len(text) > max_chars and len(h2h_block) > 1:
            h2h_block.pop()
            text = _render(h2h_block, aggregate_block)
        while len(text) > max_chars and len(aggregate_block) > 1:
            aggregate_block.pop()
            text = _render(h2h_block, aggregate_block)
        if len(text) > max_chars:
            text = (
                text[:max_chars].rsplit("\n", 1)[0]
                + "\n... [rows truncated by deterministic size bound]"
            )
        return text
    except Exception:
        return ""


def h2h_snapshot_contract_text(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    include_json: bool = False,
    max_chars: int = 60_000,
) -> str:
    """Return prompt text that binds Master/Audit to the stable H2H snapshot."""
    snapshot = load_generation_snapshot_identity(next_v)
    if not snapshot.get("available"):
        return (
            "Stable H2H snapshot unavailable or failed integrity checks. Do not "
            "read live H2H or make matchup-count claims for this generation. "
            f"Reason: {snapshot.get('reason', 'unknown')}"
        )
    lines = [
        "Stable same-cycle evaluation snapshot for this generation:",
        f"- Snapshot file: `{snapshot['h2h_relpath']}`",
        f"- Selection rows: `{snapshot.get('selection_relpath', '')}`",
        f"- Frozen ratings: `{snapshot.get('ratings_relpath', '')}`",
        f"- Frozen bot stats: `{snapshot.get('bot_stats_relpath', '')}`",
        f"- Snapshot manifest: `{snapshot['manifest_relpath']}`",
        f"- sha256: `{snapshot.get('sha256', '')}`; entries: {snapshot.get('entries', 0)}; bytes: {snapshot.get('bytes', 0)}",
        f"- Daemon cycle: save_num={((snapshot.get('cycle') or {}).get('save_num'))}; "
        f"manifest_digest=`{((snapshot.get('cycle') or {}).get('manifest_digest', ''))}`",
        "- For verbatim H2H counts in Master plans and MasterPlanAudit, use this snapshot only.",
        "- For ratings, RD, games, coverage, trends, and ranking, use the frozen selection rows only.",
        "- Live H2H/ratings/bot_stats/rating_history may drift after snapshot creation; planning and audit must ignore that drift.",
    ]
    try:
        lines.extend(["", build_h2h_prompt_summary(next_v, source_v=source_v)])
    except Exception:
        pass
    if include_json:
        try:
            text = Path(snapshot["h2h_path"]).read_text(encoding="utf-8")
        except Exception:
            text = "{}"
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [snapshot truncated for prompt budget]"
        lines.extend(["", "Snapshot JSON:", "```json", text, "```"])
    return "\n".join(lines)


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key}: {_flatten_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return "\n".join(_flatten_text(item) for item in value)
    return str(value or "")


def _extract_int(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _h2h_key_re() -> re.Pattern:
    return re.compile(
        rf"\b{re.escape(ACTIVE_BOT_PREFIX)}(\d+)\s+vs\s+{re.escape(ACTIVE_BOT_PREFIX)}(\d+)\b",
        re.IGNORECASE,
    )
_WL_RE = re.compile(r"(?<![\w.])(\d+)\s*W\s*(?:[/:\-]|,)?\s*(\d+)\s*L\b", re.IGNORECASE)
_NEXT_PAIRING_RE = re.compile(
    rf"(?:{re.escape(ACTIVE_BOT_PREFIX)}\d+|v\d+)\s+vs\s+"
    rf"(?:{re.escape(ACTIVE_BOT_PREFIX)}\d+|v\d+)",
    re.IGNORECASE,
)
_AGGREGATE_POINTER_RE = re.compile(
    r"snapshot:|bot_stats\.json|selection_snapshot\.json",
    re.IGNORECASE,
)


def _matchup_citation_window(text: str, start: int, alias: str) -> str:
    """Numbers attached to one matchup alias, not later aggregate rows.

    A raw 360-character window after the pairing name bound ``bot_stats``
    ``games=551`` onto an H2H alias whose snapshot row was ``games=49``
    (v411 / v412, 2026-09-10). Stop at the next pairing or a snapshot
    pointer so matchup accuracy and aggregate corroboration stay distinct.
    """

    chunk = text[start:start + 360]
    alias_len = len(alias)
    rest = chunk[alias_len:]
    next_pairing = _NEXT_PAIRING_RE.search(rest)
    if next_pairing:
        chunk = chunk[: alias_len + next_pairing.start()]
        rest = chunk[alias_len:]
    stop = _AGGREGATE_POINTER_RE.search(chunk)
    if stop is not None and stop.start() >= alias_len:
        chunk = chunk[: stop.start()]
    return chunk


def _h2h_key_aliases(key: str) -> list[tuple[str, str, str]]:
    """Return textual aliases and perspective for a snapshot H2H key."""
    match = _h2h_key_re().search(str(key or ""))
    if not match:
        return [(str(key or ""), "", "")]
    a_v, b_v = match.group(1), match.group(2)
    aliases = [
        (f"{bot_name(int(a_v))} vs {bot_name(int(b_v))}", a_v, b_v),
        (f"v{a_v} vs v{b_v}", a_v, b_v),
        (f"{bot_name(int(b_v))} vs {bot_name(int(a_v))}", b_v, a_v),
        (f"v{b_v} vs v{a_v}", b_v, a_v),
    ]
    seen: set[str] = set()
    deduped: list[tuple[str, str, str]] = []
    for alias, first, second in aliases:
        low = alias.lower()
        if low in seen:
            continue
        seen.add(low)
        deduped.append((alias, first, second))
    return deduped


def _pool_max_games_from_roles(bundle: dict | None) -> int:
    """Largest typed games across one loaded bundle's h2h/bot_stats/selection.

    Typing rule is the ONE shared ``agent_master_validation._strength_row_games``
    (strength-signal keys must be ints) so the two tiers can never disagree
    because one scanner accepted a float the other rejected.  The scan is the
    audit-mirror half of ``statistical_evidence_floor_errors`` tier math; the
    pre-injection table and the aggregate repair guidance reuse this exact
    scan so the advertised pointers can never disagree with the gate either.
    """
    from agent_master_validation import _strength_row_games

    best = 0
    for role in ("h2h", "bot_stats", "selection"):
        data = bundle.get(role) if isinstance(bundle, dict) else None
        if not isinstance(data, dict):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                games = _strength_row_games(node)
                if games > best:
                    best = games
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return best


def _snapshot_pool_max_games_for(next_v: int | str) -> int:
    """Largest games count across the generation snapshot's citable rows."""
    return _pool_max_games_from_roles(load_generation_evaluation_snapshot(next_v))


def _aggregate_tier_pointers(
    next_v: int | str,
    *,
    bundle: dict | None = None,
    max_refs: int = 6,
) -> tuple[int, list[tuple[str, int]]]:
    """Aggregate-tier pointers the plan can legally cite, with exact games.

    The aggregate corroboration leg of the two-tier statistical evidence bar
    needs a citation whose bound ``games`` reaches the pool-annealed aggregate
    tier (>= 200 once the pool is mature).  H2H rows cap far below that, so
    the legal citations are per-bot ``bot_stats.json`` pointers, the
    ``selection_snapshot.json#/rows`` container pointer (bound to its
    strongest row's games exactly like ``_snapshot_reference_evidence_binding``
    binds a list), and individual selection rows.  Tier math is the SAME
    shared helpers the audit mirror uses, so a pointer printed here can never
    be below the tier the gate will apply.  Returns
    ``(aggregate_tier, [(pointer, games), ...])``; the tier is 0 when the
    snapshot cannot be read (UNKNOWN — callers fail open).
    """
    from agent_master_validation import _pool_annealed_tiers

    if bundle is None:
        bundle = load_generation_evaluation_snapshot(next_v)
    if not isinstance(bundle, dict) or not bundle.get("available"):
        return 0, []
    _pool_primary, aggregate_tier = _pool_annealed_tiers(
        _pool_max_games_from_roles(bundle)
    )

    # Pointer games use the SAME rule the proposal gate's
    # ``_snapshot_reference_evidence_binding`` writes into
    # ``binding["games"]``: a raw int ``games`` scalar (the typed
    # ``_strength_row_games`` rule deliberately rejects selection rows whose
    # only companion is a float ``win_rate``, but the gate grades citation
    # games by the binding, so the advertised numbers must match the
    # binding, not the pool-max typing).
    def _row_games(node: object) -> int:
        if not isinstance(node, dict):
            return 0
        games = node.get("games")
        if isinstance(games, int) and not isinstance(games, bool):
            return games
        return 0

    # (games, priority, pointer): priority 0 = the advertised selection
    # container pointer, 1 = per-bot bot_stats rows, 2 = individual selection
    # rows.  Sorting is priority-then-games so the simplest pointer leads.
    ranked: list[tuple[int, int, str]] = []
    bot_stats = bundle.get("bot_stats")
    if isinstance(bot_stats, dict):
        for name, row in bot_stats.items():
            games = _row_games(row)
            if games < aggregate_tier or games <= 0:
                continue
            escaped = str(name).replace("~", "~0").replace("/", "~1")
            ranked.append((games, 1, f"snapshot:bot_stats.json#/{escaped}"))
    selection = bundle.get("selection")
    rows = (
        selection.get("rows")
        if isinstance(selection, dict) and isinstance(selection.get("rows"), list)
        else []
    )
    strongest = max((_row_games(row) for row in rows), default=0)
    if strongest >= aggregate_tier:
        ranked.append(
            (strongest, 0, "snapshot:selection_snapshot.json#/rows")
        )
    for index, row in enumerate(rows):
        games = _row_games(row)
        if games < aggregate_tier or games <= 0:
            continue
        ranked.append((games, 2, f"snapshot:selection_snapshot.json#/rows/{index}"))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    pointers: list[tuple[str, int]] = []
    seen: set[str] = set()
    for games, _priority, pointer in ranked:
        if pointer in seen:
            continue
        seen.add(pointer)
        pointers.append((pointer, games))
        if len(pointers) >= max_refs:
            break
    return aggregate_tier, pointers


def statistical_evidence_floor_errors(
    master_plan: Any,
    next_v: int | str,
    *,
    min_primary_games: int = 30,
    min_aggregate_games: int = 200,
) -> list[str]:
    """Two-tier statistical evidence bar on the plan's cited H2H rows.

    2026-08-16 evolution audit: 12/12 selected plans acted on n=4-56 H2H
    rows (8/12 on n<=15) — pure noise fitting. A load-bearing claim must
    cite one matchup row with games >= its per-matchup primary tier
    (30 once that matchup is mature; annealed to that matchup's best row
    while cold, floor 15) AND one row with games >= 200 as aggregate
    corroboration (per-bot rows in bot_stats.json and selection_snapshot
    rows carry 200-500 games). Rows whose cited numbers already FAIL
    validate_h2h_citations_against_snapshot are not re-litigated here —
    this check is sufficiency, that one is accuracy.

    2026-09-13: the tier math, matchup classification, and rejection token
    are shared with the proposal gate (agent_master_validation) so both
    sides emit byte-identical verdicts.
    """
    h2h = load_generation_h2h_snapshot(next_v)
    if not h2h:
        return []
    text = _flatten_text(master_plan)
    # Prefer the plan's OWN validated snapshot bindings as the citation set:
    # the proposal gate grades those same bindings (any of the 7 strength
    # files), so an H2H-alias-only derivation here made the audit demand an
    # H2H row >= 30 in pools whose only >=30 rows live in bot_stats — every
    # plan naming a matchup was then burned (static audit finding 1,
    # 2026-08-17). Alias matching remains the fallback for plans whose
    # bindings were stripped.
    bindings = None
    if isinstance(master_plan, dict):
        binding = master_plan.get("proposal_binding")
        if isinstance(binding, dict):
            raw = binding.get("snapshot_evidence")
            if isinstance(raw, list):
                bindings = [b for b in raw if isinstance(b, dict)]
    citations: list[tuple[str, int]] = []
    if bindings:
        for binding_row in bindings:
            games = binding_row.get("games")
            if isinstance(games, int) and not isinstance(games, bool):
                citations.append(
                    (str(binding_row.get("reference") or ""), int(games))
                )
    if not citations:
        for key, row in h2h.items():
            if not isinstance(row, dict):
                continue
            games = int(row.get("games", 0) or 0)
            if games <= 0:
                continue
            for alias, _first, _second in _h2h_key_aliases(str(key)):
                if not alias:
                    continue
                if re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])",
                    text,
                    re.IGNORECASE,
                ):
                    citations.append((str(key), games))
                    break
    if not citations:
        # No matchup citation at all: the accuracy validator or the proposal
        # schema owns that failure mode; sufficiency has nothing to grade.
        return []
    # Primary (matchup-row) tiers anneal per cited matchup; the aggregate tier
    # and the pool-wide fallback primary anneal on the whole-pool max. The
    # tier math, matchup classification, and rejection token are the SAME
    # shared helpers the proposal validator uses (agent_master_validation), so
    # the audit mirror and the proposal gate can never emit divergent verdicts
    # or rejection strings (AGENTS.md: one citation set, one pool-max typing
    # rule).
    from agent_master_validation import (
        _cited_sample_primary_state,
        _pool_annealed_tiers,
        format_cited_sample_rejection,
    )

    pool_primary, aggregate_tier = _pool_annealed_tiers(
        _snapshot_pool_max_games_for(next_v),
        min_primary_games=min_primary_games,
        min_aggregate_games=min_aggregate_games,
    )
    has_primary, report = _cited_sample_primary_state(
        citations, h2h, pool_primary
    )
    # Aggregate corroboration: H2H rows cap at ~58 games, so the >=200 tier
    # is necessarily a bot_stats.json / selection_snapshot.json citation —
    # detect the snapshot reference in the plan text.
    has_aggregate = bool(
        re.search(
            r"snapshot:(?:bot_stats|selection_snapshot)\.json",
            text,
        )
    )
    if has_primary and has_aggregate:
        return []
    return [
        format_cited_sample_rejection(
            matchup=report["matchup"],
            cited=report["cited"],
            best_available=report["best_available"],
            tier=report["tier"],
            aggregate_tier=aggregate_tier,
        )
    ]


# Roles of the evaluation bundle that mirror the proposal gate's seven
# strength-snapshot files (match_history_index and the manifest carry no
# citable strength rows).
_EVIDENCE_POOL_ROLES = (
    "h2h",
    "bot_stats",
    "selection",
    "ratings",
    "action_stats",
    "action_stats_per_opp",
    "replay_spotlight",
)


def evidence_floor_retry_doomed(next_v: int | str) -> dict | None:
    """Whether ANY pool row could satisfy the two statistical evidence tiers.

    2026-09-13 doomed-retry guard: after a purely statistical-floor audit
    rejection, a corrective re-plan can only succeed if the pool actually
    contains a row that could serve as a primary matchup basis (a pairing
    whose best row reaches that pairing's per-matchup tier — equivalently,
    some H2H row at or above the shared 15-game floor — or an aggregate-class
    row at or above the pool-annealed primary tier) AND a row that could
    serve as aggregate corroboration. When either side is unsatisfiable the
    retry is mathematically doomed and must not burn the second Master run.

    Returns ``None`` when the pool cannot be read (UNKNOWN never skips);
    otherwise a report dict whose ``doomed`` flags the unsatisfiable case
    and whose numbers feed the operator event.
    """
    bundle = load_generation_evaluation_snapshot(next_v)
    if not isinstance(bundle, dict) or not bundle.get("available"):
        return None
    from agent_master_validation import (
        _h2h_pair_versions,
        _matchup_primary_tier,
        _pool_annealed_tiers,
        _strength_row_games,
    )

    pair_best: dict[tuple[int, int], int] = {}
    non_matchup_max = 0
    h2h = bundle.get("h2h") if isinstance(bundle.get("h2h"), dict) else {}
    for key, row in h2h.items():
        games = _strength_row_games(row)
        if games <= 0:
            continue
        pair = _h2h_pair_versions(str(key))
        if pair is None:
            # Unparseable H2H keys classify as aggregate-class citations at
            # the gate, so they count toward the non-matchup maximum here.
            non_matchup_max = max(non_matchup_max, games)
        else:
            pair_best[pair] = max(pair_best.get(pair, 0), games)
    for role in _EVIDENCE_POOL_ROLES:
        if role == "h2h":
            continue
        data = bundle.get(role)
        if not isinstance(data, dict):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                games = _strength_row_games(node)
                if games > non_matchup_max:
                    non_matchup_max = games
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    all_max = max(
        [non_matchup_max, *pair_best.values()] if pair_best else [non_matchup_max]
    )
    pool_primary, aggregate_tier = _pool_annealed_tiers(all_max)
    primary_satisfiable = (
        any(
            best >= _matchup_primary_tier(best)
            for best in pair_best.values()
        )
        or non_matchup_max >= pool_primary
    )
    aggregate_satisfiable = all_max >= aggregate_tier
    return {
        "doomed": not (primary_satisfiable and aggregate_satisfiable),
        "primary_satisfiable": primary_satisfiable,
        "aggregate_satisfiable": aggregate_satisfiable,
        "primary_tier": pool_primary,
        "aggregate_tier": aggregate_tier,
        "best_matchup_games": max(pair_best.values(), default=0),
        "best_non_matchup_games": non_matchup_max,
    }


def validate_h2h_citations_against_snapshot(master_plan: Any, next_v: int | str) -> list[str]:
    """Detect labeled H2H count citations that disagree with the generation snapshot."""
    h2h = load_generation_h2h_snapshot(next_v)
    if not h2h:
        return []
    text = _flatten_text(master_plan)
    errors: list[str] = []
    for key, row in h2h.items():
        if not isinstance(row, dict):
            continue
        seen_spans: set[tuple[int, int]] = set()
        for alias, first_v, second_v in _h2h_key_aliases(str(key)):
            if not alias:
                continue
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])", re.IGNORECASE)
            for match in pattern.finditer(text):
                span = match.span()
                if span in seen_spans:
                    continue
                seen_spans.add(span)
                window = _matchup_citation_window(text, span[0], alias)
                cited = {
                    "games": _extract_int(r"\bgames?\s*[:=]\s*(\d+)", window),
                    "a_wins": _extract_int(r"\ba_wins\s*[:=]\s*(\d+)", window),
                    "b_wins": _extract_int(r"\bb_wins\s*[:=]\s*(\d+)", window),
                    "draws": _extract_int(r"\bdraws\s*[:=]\s*(\d+)", window),
                }
                if cited["games"] is None:
                    cited["games"] = _extract_int(r"(?<![\w.])(\d+)\s*(?:g|games|局)\b", window)

                wl_match = _WL_RE.search(window)
                if wl_match:
                    wins = int(wl_match.group(1))
                    losses = int(wl_match.group(2))
                    cited["games"] = cited["games"] if cited["games"] is not None else wins + losses
                    key_match = _h2h_key_re().search(str(key))
                    key_a = key_match.group(1) if key_match else first_v
                    if first_v == key_a:
                        cited["a_wins"] = cited["a_wins"] if cited["a_wins"] is not None else wins
                        cited["b_wins"] = cited["b_wins"] if cited["b_wins"] is not None else losses
                    else:
                        cited["a_wins"] = cited["a_wins"] if cited["a_wins"] is not None else losses
                        cited["b_wins"] = cited["b_wins"] if cited["b_wins"] is not None else wins

                for field, value in cited.items():
                    if value is None:
                        continue
                    actual = int(row.get(field, 0) or 0)
                    if value != actual:
                        errors.append(
                            f"{alias} cited {field}={value}, snapshot has {field}={actual} (key {key})"
                        )
    return errors


# ---------------------------------------------------------------------------
# Deterministic citation normalization (pre-audit)
# ---------------------------------------------------------------------------
# v451/v485 (2026-09) died at the plan audit because the final Master plan
# kept writing hallucinated H2H games/wins numbers even though the prompt
# pre-injected the exact citable rows and the rejection feedback carried the
# repair guidance.  The audit itself is correct (fail-closed), so the last
# deterministic line of defense is to REWRITE resolvable citation numbers in
# the accepted plan to the SAME frozen snapshot values the audit compares
# against, before the audit reads the plan.  The audit logic is untouched:
# after normalization a pass means the plan cites snapshot-exact numbers, and
# a residual rejection still blocks the plan exactly as before.

_CITED_FIELD_PATTERNS: tuple[tuple[str, str], ...] = (
    ("games", r"\bgames?\s*[:=]\s*(\d+)"),
    ("a_wins", r"\ba_wins\s*[:=]\s*(\d+)"),
    ("b_wins", r"\bb_wins\s*[:=]\s*(\d+)"),
    ("draws", r"\bdraws\s*[:=]\s*(\d+)"),
)
_CITED_GAMES_FALLBACK_PATTERN = r"(?<![\w.])(\d+)\s*(?:g|games|局)\b"
# Aggregate pointer citations: snapshot:bot_stats.json#/... and
# snapshot:selection_snapshot.json#/rows...  The locator stops at the same
# prose boundaries the flattened plan realistically delimits with.
_AGGREGATE_CITATION_POINTER_RE = re.compile(
    r"snapshot:((?:bot_stats|selection_snapshot)\.json)#(/[^\s\"'<>|,;)\]]*)",
    re.IGNORECASE,
)
_NORMALIZATION_REPORT_CAP = 64


def _row_int(row: dict, field: str) -> int:
    return int(row.get(field, 0) or 0)


def _pointer_node(bundle: dict, filename: str, locator: str) -> object:
    """Resolve one aggregate pointer against the loaded snapshot bundle.

    Mirrors ``agent_master_validation._snapshot_reference_evidence_binding``
    resolution (same ~1/~0 unescaping, same strongest-row rule for list
    containers) but reads the already-parsed bundle instead of reopening the
    snapshot files, so normalization and the audit see identical bytes.
    """
    role = {
        "bot_stats.json": "bot_stats",
        "selection_snapshot.json": "selection",
    }.get(filename.lower())
    if role is None:
        return None
    node: object = bundle.get(role)
    if locator == "/":
        return node
    for raw_part in locator[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        elif isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                return None
            node = node[int(part)]
        else:
            return None
    return node


def _pointer_resolved_games(bundle: dict, filename: str, locator: str) -> int | None:
    """Sample size an aggregate pointer binds (``None`` when unresolvable).

    Dict rows bind their own ``games``; list containers (e.g.
    ``selection_snapshot.json#/rows``) bind the strongest element's raw int
    ``games`` — the exact rule ``_snapshot_reference_evidence_binding``
    applies when it creates the proposal's own bindings, so normalization
    and the proposal gate can never disagree on a pointer's games.
    """
    node = _pointer_node(bundle, filename, locator)
    if isinstance(node, dict):
        games = node.get("games")
        if isinstance(games, int) and not isinstance(games, bool):
            return games
        return None
    if isinstance(node, list):
        strongest = 0
        for element in node:
            if not isinstance(element, dict):
                continue
            value = element.get("games")
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value > strongest
            ):
                strongest = value
        return strongest if strongest > 0 else None
    return None


def _collect_aggregate_games_rewrites(
    text: str,
    bundle: dict,
    ops: list[tuple[tuple[int, int], str, dict]],
) -> None:
    """Queue ``games=`` digit rewrites under resolvable aggregate pointers."""
    for match in _AGGREGATE_CITATION_POINTER_RE.finditer(text):
        filename, locator = match.group(1), match.group(2)
        resolved = _pointer_resolved_games(bundle, filename, locator)
        if resolved is None:
            # 解析不到行: leave the citation untouched for the audit.
            continue
        window = _matchup_citation_window(text, match.start(), match.group(0))
        window_text = text[match.start(): match.start() + len(window)]
        pattern = re.compile(_CITED_FIELD_PATTERNS[0][1], re.IGNORECASE)
        field_match = pattern.search(window_text)
        if field_match is None:
            field_match = re.search(
                _CITED_GAMES_FALLBACK_PATTERN, window_text, re.IGNORECASE
            )
        if field_match is None:
            continue
        try:
            cited_games = int(field_match.group(1))
        except (TypeError, ValueError):
            continue
        if cited_games == resolved:
            continue
        span = (
            match.start() + field_match.start(1),
            match.start() + field_match.end(1),
        )
        ops.append((
            span,
            str(resolved),
            {
                "kind": "aggregate_pointer",
                "pointer": f"snapshot:{filename}#{locator}",
                "field": "games",
                "from": cited_games,
                "to": resolved,
            },
        ))


def _collect_matchup_rewrites(
    text: str,
    key: str,
    row: dict,
    matchup_citable: bool,
    ops: list[tuple[tuple[int, int], str, dict]],
) -> None:
    """Queue digit rewrites for one H2H row's citations inside one leaf.

    The extraction mirrors ``validate_h2h_citations_against_snapshot`` field
    by field (same patterns, same window, same W/L perspective fill) so a
    rewritten citation is exactly one the audit would otherwise reject.
    """
    key_match = _h2h_key_re().search(str(key))
    key_a = key_match.group(1) if key_match else None
    row_values = {
        field: _row_int(row, field)
        for field, _pattern in _CITED_FIELD_PATTERNS
    }
    seen_spans: set[tuple[int, int]] = set()
    for alias, first_v, _second_v in _h2h_key_aliases(str(key)):
        if not alias:
            continue
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        for alias_match in pattern.finditer(text):
            span = alias_match.span()
            if span in seen_spans:
                continue
            seen_spans.add(span)
            window = _matchup_citation_window(text, span[0], alias)
            window_start = span[0]
            window_text = text[window_start: window_start + len(window)]

            explicit: dict[str, tuple[int, int, int] | None] = {}
            cited: dict[str, int | None] = {}
            for field, field_pattern in _CITED_FIELD_PATTERNS:
                field_match = re.search(field_pattern, window_text, re.IGNORECASE)
                if field_match is None:
                    explicit[field] = None
                    cited[field] = None
                    continue
                try:
                    value = int(field_match.group(1))
                except (TypeError, ValueError):
                    field_match = None
                    value = None
                explicit[field] = (
                    (field_match.start(1), field_match.end(1), value)
                    if field_match is not None
                    else None
                )
                cited[field] = value
            if cited["games"] is None:
                fallback = re.search(
                    _CITED_GAMES_FALLBACK_PATTERN, window_text, re.IGNORECASE
                )
                if fallback is not None:
                    try:
                        cited["games"] = int(fallback.group(1))
                        explicit["games"] = (
                            fallback.start(1), fallback.end(1), cited["games"],
                        )
                    except (TypeError, ValueError):
                        pass

            # W/L fill: identical perspective mapping to the audit.
            wl_match = _WL_RE.search(window_text)
            wl_rewrite: tuple[int, int, int, int, int, int] | None = None
            if wl_match:
                wins = int(wl_match.group(1))
                losses = int(wl_match.group(2))
                key_first = key_a if key_a is not None else first_v
                if first_v == key_first:
                    target_w, target_l = row_values["a_wins"], row_values["b_wins"]
                    if cited["a_wins"] is None:
                        cited["a_wins"] = wins
                    if cited["b_wins"] is None:
                        cited["b_wins"] = losses
                else:
                    target_w, target_l = row_values["b_wins"], row_values["a_wins"]
                    if cited["a_wins"] is None:
                        cited["a_wins"] = losses
                    if cited["b_wins"] is None:
                        cited["b_wins"] = wins
                if cited["games"] is None:
                    cited["games"] = wins + losses
                wl_contributed = (
                    explicit["games"] is None
                    or explicit["a_wins"] is None
                    or explicit["b_wins"] is None
                )
                if wl_contributed and (wins, losses) != (target_w, target_l):
                    wl_rewrite = (
                        wl_match.start(1), wl_match.end(1), target_w,
                        wl_match.start(2), wl_match.end(2), target_l,
                    )

            if not matchup_citable:
                # 行本身不满足 per-matchup 门槛: leave the citation untouched
                # so the audit keeps rejecting it exactly as before.
                continue

            for field, _field_pattern in _CITED_FIELD_PATTERNS:
                bound = explicit[field]
                if bound is None:
                    continue
                start, end, value = bound
                if value == row_values[field]:
                    continue
                ops.append((
                    (window_start + start, window_start + end),
                    str(row_values[field]),
                    {
                        "kind": "h2h_matchup",
                        "matchup": alias,
                        "field": field,
                        "from": value,
                        "to": row_values[field],
                    },
                ))
            if wl_rewrite is not None:
                w_start, w_end, target_w, l_start, l_end, target_l = wl_rewrite
                ops.append((
                    (window_start + w_start, window_start + w_end),
                    str(target_w),
                    {
                        "kind": "h2h_matchup",
                        "matchup": alias,
                        "field": "wins(W)",
                        "from": int(wl_match.group(1)),
                        "to": target_w,
                    },
                ))
                ops.append((
                    (window_start + l_start, window_start + l_end),
                    str(target_l),
                    {
                        "kind": "h2h_matchup",
                        "matchup": alias,
                        "field": "losses(L)",
                        "from": int(wl_match.group(2)),
                        "to": target_l,
                    },
                ))


def _flatten_marked(
    value: Any,
    pieces: list[tuple[int, int, Any, Any, str, str]],
    start: int,
    path: str = "",
    parent: Any = None,
    key: Any = None,
) -> tuple[str, int]:
    """Render one plan node exactly like :func:`_flatten_text`, with spans.

    Returns ``(text, end_offset)`` where offsets are absolute in the final
    joined document.  Every non-empty scalar leaf is recorded in ``pieces``
    as ``(start, end, parent, key, path, rendered_text)`` so a digit span
    found in the joined text can be mapped back to the exact container slot
    that rendered it (a string leaf -> in-string rewrite; an int leaf -> the
    int is reassigned).  The rendering must stay byte-identical to
    ``_flatten_text`` — the audit's citation windows are computed over that
    exact joined form.
    """
    if isinstance(value, dict):
        lines: list[str] = []
        pos = start
        for child_key, item in value.items():
            prefix = f"{child_key}: "
            child_path = f"{path}.{child_key}" if path else str(child_key)
            child_text, child_end = _flatten_marked(
                item, pieces, pos + len(prefix), child_path, value, child_key
            )
            lines.append(prefix + child_text)
            pos = child_end + 1  # +1 joins the next rendered line
        text = "\n".join(lines)
        return text, start + len(text)
    if isinstance(value, (list, tuple, set)):
        lines = []
        pos = start
        for index, item in enumerate(value):
            child_path = f"{path}[{index}]"
            child_text, child_end = _flatten_marked(
                item, pieces, pos, child_path, value, index
            )
            lines.append(child_text)
            pos = child_end + 1
        text = "\n".join(lines)
        return text, start + len(text)
    text = str(value or "")
    if text and parent is not None:
        pieces.append((start, start + len(text), parent, key, path, text))
    return text, start + len(text)


def _apply_span_rewrites(
    pieces: list[tuple[int, int, Any, Any, str, str]],
    ops: list[tuple[tuple[int, int], str, dict]],
    report: list[dict],
) -> int:
    """Apply digit rewrites to the plan leaves the flagged spans rendered from.

    Overlapping ops (a digit span claimed by two windows) apply once — the
    leftmost wins — and any residual mismatch stays for the audit to reject.
    String leaves are rebuilt in place; int leaves (the flattened ``games:``
    lines of structured binding objects) have their number reassigned only
    when the flagged span is exactly the leaf's rendered digits.  Returns
    the number of applied rewrites (the report list itself is capped; the
    count is not).
    """
    piece_starts = [piece[0] for piece in pieces]
    applied: list[tuple[int, int]] = []
    per_leaf: dict[int, list[tuple[tuple[int, int], str, dict]]] = {}
    for (start, end), replacement, meta in sorted(
        ops, key=lambda op: (op[0][0], op[0][1])
    ):
        if any(start < a_end and end > a_start for a_start, a_end in applied):
            continue
        index = bisect.bisect_right(piece_starts, start) - 1
        if index < 0:
            continue
        piece = pieces[index]
        if not (piece[0] <= start and end <= piece[1]):
            continue
        applied.append((start, end))
        per_leaf.setdefault(index, []).append(
            ((start - piece[0], end - piece[0]), replacement, meta)
        )
    count = 0
    for index, leaf_ops in per_leaf.items():
        _start, _end, parent, key, path, rendered = pieces[index]
        value = parent[key]
        if isinstance(value, str):
            new_text = value
            for (rel_start, rel_end), replacement, meta in leaf_ops:
                new_text = (
                    new_text[:rel_start] + replacement + new_text[rel_end:]
                )
            if new_text != value:
                parent[key] = new_text
                for _span, _replacement, meta in leaf_ops:
                    if len(report) < _NORMALIZATION_REPORT_CAP:
                        report.append({"path": path, **meta})
                count += len(leaf_ops)
        elif (
            isinstance(value, int)
            and not isinstance(value, bool)
            and rendered.isdigit()
            and len(leaf_ops) == 1
            and leaf_ops[0][0] == (0, len(rendered))
            and str(int(leaf_ops[0][1])) == leaf_ops[0][1]
        ):
            replacement = int(leaf_ops[0][1])
            if replacement != value:
                parent[key] = replacement
                if len(report) < _NORMALIZATION_REPORT_CAP:
                    report.append(
                        {"path": path, **leaf_ops[0][2]}
                    )
                count += 1
    return count


def normalize_master_plan_citations(
    master_plan: Any,
    next_v: int | str,
    *,
    source_v: int | str | None = None,
) -> dict:
    """Rewrite resolvable citation numbers in an accepted plan to snapshot values.

    Runs at the plan-accepted/audit-start seam on the SAME in-memory plan the
    audit then reads, using the SAME frozen generation evidence snapshot the
    audit validates against (never live results).  The plan is flattened to
    the byte-identical text ``validate_h2h_citations_against_snapshot`` sees
    (``_flatten_text``), so every citation the audit can attribute — prose
    windows AND structured binding objects (``{"games": 259, ...}`` int
    leaves inside ``proposal_ensemble`` / ``proposal_binding``
    ``snapshot_evidence`` lists, which the joined text renders as
    ``games: 259`` lines inside cross-object pair windows) — is normalized
    with the audit's own attribution priority: numbers inside a pairing
    window (before its truncating aggregate pointer / next pairing) belong
    to the pair row, numbers under a resolvable aggregate pointer belong to
    that pointer's row.  Rules, strictly fail-closed:

    - An H2H matchup citation is normalized only when it resolves to a real
      snapshot row (both wire directions, shared ``_h2h_pair_versions``
      mapping) AND that row's matchup satisfies the per-matchup primary tier.
      A citation that resolves to no row, or whose row cannot legally serve
      as primary evidence, is left untouched for the audit to reject.
    - An aggregate pointer citation (``snapshot:bot_stats.json#/...`` /
      ``snapshot:selection_snapshot.json#/rows...``) has its bound ``games``
      normalized only when the pointer resolves against the bundle; an
      unresolvable pointer is untouched.
    - Only the compared citation digits change.  ``node_sha256`` /
      ``projection_sha256`` / ``resolved_projection`` bytes are never
      touched (the audit's patterns cannot match their quoted/hex forms).
      The ``proposal_binding`` / ``proposal_ensemble`` snapshot-evidence int
      leaves ARE rewritten because the audit demonstrably compares them
      (the v485 ``cited games=259`` rejection came from exactly such a
      leaf); nothing in the non-bootstrap path re-derives those bindings
      after acceptance, and the blueprint equality check that re-derives
      ``proposal_binding`` from the ensemble runs only in the bootstrap
      mode where normalization is skipped (no strength pool), so both
      structures stay mutually consistent when rewritten to the same row
      values.
    - Every replacement is recorded in the returned report; the audit itself
      is untouched, so a residual rejection still blocks the plan.

    Returns a report dict; never raises for plan-shape reasons (fail-open to
    the audit, which remains the gate).
    """
    report: dict = {
        "available": False,
        "total": 0,
        "normalizations": [],
    }
    if not isinstance(master_plan, dict):
        return report
    try:
        h2h = load_generation_h2h_snapshot(next_v)
        bundle = load_generation_evaluation_snapshot(next_v)
    except Exception as exc:  # pragma: no cover - defensive, audit still gates
        report["reason"] = f"snapshot_read_failed:{type(exc).__name__}"
        return report
    if not h2h or not isinstance(bundle, dict) or not bundle.get("available"):
        return report
    report["available"] = True

    from agent_master_validation import (
        _h2h_pair_versions,
        _matchup_max_games_rows,
        _matchup_primary_tier,
    )

    # A matchup row may only carry a normalized citation when the matchup can
    # legally serve as primary evidence; sub-tier rows stay untouched so the
    # audit keeps rejecting them (normalization never legitimizes a citation).
    key_citable: dict[str, bool] = {}
    for key, row in h2h.items():
        if not isinstance(row, dict):
            continue
        pair = _h2h_pair_versions(str(key))
        if pair is None:
            key_citable[str(key)] = False
            continue
        best = _matchup_max_games_rows(h2h, pair)
        key_citable[str(key)] = best >= _matchup_primary_tier(best)

    pieces: list[tuple[int, int, Any, Any, str, str]] = []
    text, _end = _flatten_marked(master_plan, pieces, 0)
    ops: list[tuple[tuple[int, int], str, dict]] = []
    for key, row in h2h.items():
        if not isinstance(row, dict) or not key_citable.get(str(key), False):
            continue
        _collect_matchup_rewrites(
            text, str(key), row, key_citable[str(key)], ops
        )
    _collect_aggregate_games_rewrites(text, bundle, ops)
    normalizations: list[dict] = report["normalizations"]
    report["total"] = _apply_span_rewrites(pieces, ops, normalizations)
    if source_v is not None:
        report["source_v"] = str(source_v)
    return report
