"""Match-replay storage subsystem for elo_daemon.

Extracted as a cohesive business cluster; ``elo_daemon.py`` retains thin
delegate shells so external ``from elo_daemon import <name>`` and
``monkeypatch.setattr(elo_daemon, "<name>", ...)`` keep resolving.

Business responsibility (single cohesive domain):
* Public ``save_match_replay`` entry point.
* Safe replay-directory resolution (``_ensure_safe_replay_directory``).
* Cycle-locked replay persistence
  (``_save_match_replay_under_cycle_lock``) with its nested helpers.
* Retention/garbage-collection of old replays (``cleanup_old_replays``) and
  its nested reference-collecting helpers.

Filesystem-bound storage: writes/reads replay bytes and prunes only
evidence-unreferenced files.

Cross-references to symbols that remain in ``elo_daemon`` (the
``REPLAY_DIR`` / ``RESULTS_DIR`` / ``BOTS_DIR`` / ``MATCH_HISTORY_FILE``
constants, the ``MAX_REPLAY_FILES`` cap, and the ``log`` logger) are reached
through ``_ed.<name>`` so that test monkeypatches on
``elo_daemon.<name>`` propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_ed.<name>(...)`` so monkeypatches on
``elo_daemon.<name>`` propagate even when both call sites now live in
this companion.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime
from pathlib import Path

import elo_daemon as _ed  # for cross-refs


def save_match_replay(
    a,
    b,
    wins_a,
    wins_b,
    draws,
    replay_data,
    net_chips_samples=None,
    strength_sample_unit=None,
    expected_evaluation_identity_digest=None,
    expected_native_match_timing_plan=None,
    stage_only=False,
):
    """Atomically admit replay/history against an evaluator identity epoch."""
    from evaluation_bundle import evaluation_cycle_lock

    with evaluation_cycle_lock(_ed.RESULTS_DIR, exclusive=False):
        return _ed._save_match_replay_under_cycle_lock(
            a,
            b,
            wins_a,
            wins_b,
            draws,
            replay_data,
            net_chips_samples,
            strength_sample_unit,
            expected_evaluation_identity_digest,
            expected_native_match_timing_plan,
            stage_only,
        )



def _ensure_safe_replay_directory(path: Path) -> Path:
    """Create one replay directory without accepting a symlink boundary."""

    try:
        path.mkdir(parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"replay directory is unavailable: {path}") from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"replay directory is unsafe: {path}")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"replay directory cannot be resolved: {path}") from exc



def _save_match_replay_under_cycle_lock(
    a,
    b,
    wins_a,
    wins_b,
    draws,
    replay_data,
    net_chips_samples=None,
    strength_sample_unit=None,
    expected_evaluation_identity_digest=None,
    expected_native_match_timing_plan=None,
    stage_only=False,
):
    from bot_namespace import EVALUATION_EPOCH
    from evaluation_data_identity import current_evaluation_digest

    # This API is the only producer for rating/H2H history.  Keeping a
    # diagnostic or partial receipt in the same append-only namespace would
    # invite a later caller to mistake it for strength evidence, so reject it
    # before creating either a pending file or a history row.
    if strength_sample_unit != "70_hand_match":
        raise ValueError(
            "rating replay admission requires an exact 70_hand_match strength sample"
        )
    if (
        not isinstance(a, str)
        or not isinstance(b, str)
        or a == b
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (wins_a, wins_b, draws)
        )
    ):
        raise ValueError("rating replay strength header is invalid")

    evaluation_identity_digest = current_evaluation_digest(_ed.RESULTS_DIR)
    if (
        expected_evaluation_identity_digest is not None
        and str(expected_evaluation_identity_digest) != evaluation_identity_digest
    ):
        raise RuntimeError(
            "evaluation identity changed while match was in flight; result is not admitted"
        )
    replay_root = _ed._ensure_safe_replay_directory(_ed.REPLAY_DIR)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    fname = f"{timestamp}_{a}_vs_{b}.json"
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (net_chips_samples or [])
    ):
        raise ValueError("70-hand strength replay net-chip samples must be integers")
    net_chips_values = list(net_chips_samples or [])
    from strength_order import summarize_70_hand_net_chips
    from national_native import (
        _artifact_execution_is_valid,
        require_native_match_timing_plan,
        validate_native_match_timing_evidence,
    )
    from bot_artifact import hash_path

    if expected_native_match_timing_plan is None:
        raise ValueError("70-hand strength replay timing plan is missing")
    native_timing_plan = require_native_match_timing_plan(
        expected_native_match_timing_plan,
        hands=70,
        requested_timeout_sec=None,
    )
    expected_artifacts = None

    if not net_chips_values:
        raise ValueError("70-hand strength replay must contain at least one sample")
    if not isinstance(replay_data, list) or len(replay_data) != len(net_chips_values):
        raise ValueError("70-hand strength replay rows disagree with sample count")
    for index, replay in enumerate(replay_data):
        if not isinstance(replay, dict):
            raise ValueError(f"70-hand strength replay {index} is not an object")
        if int(replay.get("hands_played", 0) or 0) != 70:
            raise ValueError(f"70-hand strength replay {index} is incomplete")
        if replay.get("passed_compliance") is not True:
            raise ValueError(f"70-hand strength replay {index} failed compliance")
        timing_issues = validate_native_match_timing_evidence(
            replay,
            timing_plan=native_timing_plan,
        )
        if timing_issues:
            raise ValueError(
                f"70-hand strength replay {index} timing evidence invalid:"
                + ";".join(timing_issues)
            )
        if expected_artifacts is None:
            expected_artifacts = {
                a: hash_path(_ed.BOTS_DIR / a),
                b: hash_path(_ed.BOTS_DIR / b),
            }
        if not _artifact_execution_is_valid(
            replay.get("artifact_execution"),
            expected_artifacts,
        ):
            raise ValueError(
                f"70-hand strength replay {index} has invalid artifact execution identity"
            )
    strength_summary = summarize_70_hand_net_chips(net_chips_values)
    if (
        strength_summary["positive_matches"] != int(wins_a)
        or strength_summary["negative_matches"] != int(wins_b)
        or strength_summary["zero_matches"] != int(draws)
    ):
        raise ValueError("70-hand net-chip samples disagree with recorded match outcomes")
    match_data = {
        "replay_schema_version": 1,
        "id": fname,
        "timestamp": timestamp,
        "execution_mode": "native_tcp",
        "evaluation_epoch": EVALUATION_EPOCH,
        "bot0": a,
        "bot1": b,
        "bot0_wins": wins_a,
        "bot1_wins": wins_b,
        "draws": draws,
        "evaluation_identity_digest": evaluation_identity_digest,
        "strength_sample_unit": strength_sample_unit,
        "hands_per_strength_sample": 70 if strength_summary is not None else None,
        "strength_admitted": strength_summary is not None,
        "strength_complete": strength_summary is not None,
        "strength_compliance_passed": strength_summary is not None,
        "strength_sample_count": strength_summary.get("samples", 0) if strength_summary else 0,
        "net_chips_bot0": net_chips_values,
        "strength_order": strength_summary,
        "native_match_timing_plan": (
            native_timing_plan.snapshot() if native_timing_plan is not None else None
        ),
        "native_match_timing_plan_digest": (
            native_timing_plan.digest() if native_timing_plan is not None else None
        ),
        "games": replay_data,
    }

    # Stage one is a complete raw-envelope validation, not merely a claim that
    # a worker returned 70.  This checks all 70 settlement/hand records,
    # current identity, exact timing plan, sample outcomes, and the strict
    # execution identity grammar before bytes can enter `.pending`.
    from replay_analysis import validate_native_replay

    staged_validation = validate_native_replay(
        match_data,
        expected_evaluation_identity_digest=evaluation_identity_digest,
        expected_replay_id=fname,
    )
    if not staged_validation.accepted:
        raise ValueError(
            "70-hand strength replay strict validation failed:"
            + str(staged_validation.reason)
        )
    if dict(staged_validation.artifact_hashes) != expected_artifacts:
        raise ValueError(
            "70-hand strength replay artifact identity does not match current bot bytes"
        )

    replay_parent = replay_root / ".pending" if stage_only else replay_root
    replay_parent = _ed._ensure_safe_replay_directory(replay_parent)
    if replay_parent != replay_root and replay_parent.parent != replay_root:
        raise RuntimeError("staged replay directory escapes replay root")
    replay_path = replay_parent / fname
    try:
        replay_bytes = json.dumps(match_data, ensure_ascii=False).encode("utf-8")
        # Timestamp collisions are not a reason to overwrite an existing
        # evidence file (which could be a hostile symlink or stale receipt).
        with open(replay_path, "xb") as f:
            f.write(replay_bytes)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        raise
    replay_sha256 = hashlib.sha256(replay_bytes).hexdigest()

    summary = {
        "id": fname,
        "timestamp": timestamp,
        "execution_mode": "native_tcp",
        "evaluation_epoch": EVALUATION_EPOCH,
        "bot0": a,
        "bot1": b,
        "bot0_wins": wins_a,
        "bot1_wins": wins_b,
        "draws": draws,
        "evaluation_identity_digest": evaluation_identity_digest,
        "strength_sample_unit": strength_sample_unit,
        "hands_per_strength_sample": 70 if strength_summary is not None else None,
        "strength_admitted": strength_summary is not None,
        "strength_complete": strength_summary is not None,
        "strength_compliance_passed": strength_summary is not None,
        "strength_sample_count": strength_summary.get("samples", 0) if strength_summary else 0,
        "net_chips_bot0": net_chips_values,
        "strength_order": strength_summary,
        "native_match_timing_plan": (
            native_timing_plan.snapshot() if native_timing_plan is not None else None
        ),
        "native_match_timing_plan_digest": (
            native_timing_plan.digest() if native_timing_plan is not None else None
        ),
        # The append-only history is only a projection.  It is never enough on
        # its own to influence strength: consumers must reopen these exact raw
        # bytes and validate the hash plus native replay contract.
        "replay_sha256": replay_sha256,
    }
    if stage_only:
        return {
            "pending_path": str(replay_path),
            "filename": fname,
            "summary": summary,
            "evaluation_identity_digest": evaluation_identity_digest,
            "replay_sha256": replay_sha256,
            "replay_bytes": len(replay_bytes),
        }

    try:
        os.makedirs(_ed.RESULTS_DIR, exist_ok=True)
        _ed.append_locked_jsonl(_ed.MATCH_HISTORY_FILE, summary)
    except Exception as e:
        _ed.log.warning("Match history write failed: %s", e)
        try:
            replay_path.unlink()
        except OSError:
            pass
        raise

    return fname



def cleanup_old_replays():
    """Prune only replays that no retained strength/evidence row can cite.

    A count cap is an operational preference, never permission to delete raw
    bytes behind an admitted match-history row or a retained immutable cycle.
    When all old files remain evidence-referenced we keep them and let normal
    cycle/history retention decide when they become removable.
    """

    try:
        replay_root = _ed._ensure_safe_replay_directory(_ed.REPLAY_DIR)
    except RuntimeError:
        return
    referenced: set[str] = set()

    def safe_replay_id(value: object) -> str | None:
        if (
            not isinstance(value, str)
            or not value.endswith(".json")
            or not value
            or "/" in value
            or "\\" in value
            or Path(value).name != value
            or value.startswith(".")
        ):
            return None
        return value

    def collect_references(path: Path):
        try:
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                return
            from evolution_infra import locked_file

            with locked_file(path, "r", encoding="utf-8") as reader:
                lines = list(reader)
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return
        for line in lines:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            replay_id = safe_replay_id(row.get("id") if isinstance(row, dict) else None)
            if replay_id is not None:
                referenced.add(replay_id)

    def regular_bytes(path: Path) -> bytes | None:
        """Read a stable regular file without following a snapshot symlink."""

        try:
            before = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(before.st_mode):
                return None
            payload = path.read_bytes()
            after = path.lstat()
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                return None
            return payload
        except OSError:
            return None

    def collect_verified_snapshot_references() -> None:
        """Keep only references bound by a complete snapshot manifest.

        Generation snapshots are immutable prompt evidence.  They are not
        allowed to keep arbitrary names alive merely because a loose JSON file
        says so; both the manifest and the two replay-reference payloads must
        pass their own digest/size contracts first.
        """

        snapshots_root = Path(_ed.RESULTS_DIR)
        try:
            generations = list(snapshots_root.iterdir())
        except OSError:
            return
        for generation in generations:
            if (
                generation.is_symlink()
                or not generation.is_dir()
                or not generation.name.startswith("v")
                or not generation.name[1:].isdigit()
            ):
                continue
            snapshot_dir = generation / "evidence_snapshot"
            try:
                snapshot_info = snapshot_dir.lstat()
            except OSError:
                continue
            if snapshot_dir.is_symlink() or not stat.S_ISDIR(snapshot_info.st_mode):
                continue
            manifest_bytes = regular_bytes(snapshot_dir / "manifest.json")
            if manifest_bytes is None:
                continue
            try:
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            claimed_digest = manifest.get("manifest_digest")
            unsigned = {
                key: value for key, value in manifest.items()
                if key != "manifest_digest"
            }
            expected_digest = hashlib.sha256(
                json.dumps(
                    unsigned,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if claimed_digest != expected_digest:
                continue
            identity = manifest.get("evaluation_identity_digest")
            if (
                not isinstance(identity, str)
                or len(identity) != 64
                or any(ch not in "0123456789abcdef" for ch in identity)
            ):
                continue
            contracts = manifest.get("files")
            cycle = manifest.get("cycle")
            if not isinstance(contracts, dict) or not isinstance(cycle, dict):
                continue

            try:
                from evidence_snapshot import (
                    SNAPSHOT_FILES,
                    SNAPSHOT_SCHEMA_VERSION,
                )
            except Exception:
                continue
            if manifest.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
                continue

            parsed_payloads: dict[str, dict] = {}
            valid = True
            for role, filename in SNAPSHOT_FILES.items():
                contract = contracts.get(role)
                if not isinstance(contract, dict) or contract.get("filename") != filename:
                    valid = False
                    break
                payload = regular_bytes(snapshot_dir / filename)
                if payload is None:
                    valid = False
                    break
                if (
                    contract.get("sha256") != hashlib.sha256(payload).hexdigest()
                    or contract.get("bytes") != len(payload)
                ):
                    valid = False
                    break
                try:
                    parsed = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    valid = False
                    break
                if not isinstance(parsed, dict):
                    valid = False
                    break
                parsed_payloads[role] = parsed
            if not valid:
                continue

            history_index = parsed_payloads["match_history_index"]
            spotlight = parsed_payloads["replay_spotlight"]
            if (
                history_index.get("evaluation_identity_digest") != identity
                or spotlight.get("evaluation_identity_digest") != identity
                or history_index.get("cycle_manifest_digest")
                != cycle.get("manifest_digest")
            ):
                continue
            replay_ids = history_index.get("replay_ids")
            entries = history_index.get("entries")
            source_replays = spotlight.get("source_replays")
            citations = spotlight.get("citations")
            if (
                not isinstance(replay_ids, list)
                or not isinstance(entries, list)
                or not isinstance(source_replays, dict)
                or not isinstance(citations, list)
            ):
                continue
            if (
                contracts["match_history_index"].get("entries") != len(entries)
                or contracts["replay_spotlight"].get("entries") != len(citations)
            ):
                continue
            safe_ids = [safe_replay_id(value) for value in replay_ids]
            if any(value is None for value in safe_ids) or len(set(safe_ids)) != len(safe_ids):
                continue
            entry_ids = {
                safe_replay_id(entry.get("id"))
                for entry in entries
                if isinstance(entry, dict)
            }
            if None in entry_ids or entry_ids != set(safe_ids):
                continue
            source_ids: set[str] = set()
            for replay_id, source in source_replays.items():
                safe_id = safe_replay_id(replay_id)
                source_digest = source.get("sha256") if isinstance(source, dict) else None
                if (
                    safe_id is None
                    or safe_id not in set(safe_ids)
                    or not isinstance(source_digest, str)
                    or len(source_digest) != 64
                    or any(ch not in "0123456789abcdef" for ch in source_digest)
                ):
                    valid = False
                    break
                source_ids.add(safe_id)
            if not valid:
                continue
            for citation in citations:
                citation_id = safe_replay_id(
                    citation.get("replay_file") if isinstance(citation, dict) else None
                )
                if citation_id is None or citation_id not in source_ids:
                    valid = False
                    break
            if valid:
                referenced.update(safe_ids)

    collect_references(Path(_ed.MATCH_HISTORY_FILE))
    try:
        from evaluation_bundle import CYCLES_DIRNAME

        cycles_root = Path(_ed.RESULTS_DIR) / CYCLES_DIRNAME
        if cycles_root.is_dir() and not cycles_root.is_symlink():
            for cycle in cycles_root.iterdir():
                if cycle.is_dir() and not cycle.is_symlink():
                    collect_references(cycle / "match_history.jsonl")
    except OSError:
        pass
    collect_verified_snapshot_references()
    files = sorted(
        (
            path
            for path in replay_root.iterdir()
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix == ".json"
                and not path.name.startswith(".")
            )
        ),
        key=lambda f: f.name,
    )
    if len(files) > _ed.MAX_REPLAY_FILES:
        removable = [path for path in files if path.name not in referenced]
        for old_file in removable[: max(0, len(files) - _ed.MAX_REPLAY_FILES)]:
            old_file.unlink()



