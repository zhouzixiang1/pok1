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


def h2h_citation_repair_guidance(
    next_v: int | str,
    citation_errors: list[str],
    *,
    source_v: int | str | None = None,
    max_rows: int = 12,
) -> str:
    """Return concrete snapshot rows to repair rejected H2H citations.

    Audit rejection feedback is often too negative ("the numbers are wrong")
    without giving the Master a replacement fact. This helper maps citation
    errors back to exact snapshot rows so the retry prompt contains the row key
    and counts to use verbatim.
    """
    h2h = load_generation_h2h_snapshot(next_v)
    if not h2h or not citation_errors:
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

    rows: list[str] = []
    for key in wanted[:max_rows]:
        row = h2h.get(key)
        if not isinstance(row, dict):
            continue
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
        rows.append(line)

    if not rows:
        return ""
    return "\n".join([
        "Use these exact stable snapshot rows to repair the rejected H2H citations:",
        *rows,
        "Do not replace them with live H2H, match_history, replay-window, or daemon-updated counts.",
    ])


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
    cite one matchup row with games >= 30 (primary) AND one row with
    games >= 200 (aggregate corroboration; per-bot rows in bot_stats.json
    and selection_snapshot rows carry 200-500 games). Rows whose cited
    numbers already FAIL validate_h2h_citations_against_snapshot are not
    re-litigated here — this check is sufficiency, that one is accuracy.
    """
    h2h = load_generation_h2h_snapshot(next_v)
    if not h2h:
        return []
    text = _flatten_text(master_plan)
    cited_games: list[int] = []
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
                cited_games.append(games)
                break
    if not cited_games:
        # No matchup citation at all: the accuracy validator or the proposal
        # schema owns that failure mode; sufficiency has nothing to grade.
        return []
    has_primary = any(g >= min_primary_games for g in cited_games)
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
    top = sorted(set(cited_games), reverse=True)[:4]
    missing = []
    if not has_primary:
        missing.append(f"primary matchup row games >= {min_primary_games}")
    if not has_aggregate:
        missing.append(
            "aggregate corroboration (snapshot:bot_stats.json or "
            "snapshot:selection_snapshot.json reference)"
        )
    return [
        "statistical evidence bar not met: cited matchup rows' games="
        f"{top}; need {' AND '.join(missing)}"
    ]


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
                window = text[span[0]:span[0] + 360]
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
