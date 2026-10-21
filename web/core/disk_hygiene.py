"""Bounded runtime-artifact janitor for the evolution control plane.

A 40G cloud VM filled to ENOSPC because live JSONL/caches grow without a
process-lifetime bound: post-publication archive rotation *copies* cold
prefixes and never truncates live ``events.jsonl``, saturator transcripts
accumulate, and abandoned ``results/vN`` trees plus orphan
``draft_candidates/`` stay forever.

This janitor deletes only **non-authority** caches and stale generation
trees. It never truncates or unlinks:

- live/primary checkpoints (``pipeline_state.json``)
- abandon ledger, evaluation cycle pointer, ratings, H2H, match_history
- ``events.jsonl`` / ``llm_costs.jsonl`` / ``worker_failures.jsonl``
  (append-only rotation authority)
- ``bots/`` products, ``strict_invocations/``, handoff records

Two bounded-retention additions (T7, 2026-10-15):

- **Orphan never-published ``vN`` trees.** ``results/vN`` trees whose
  generation was prepared but never published, never abandoned (no abandon
  ledger row), unreferenced by any live surface (primary/draft/consumer
  checkpoints, non-terminal Slice-2b lifecycle rows) and older than
  ``POK_ORPHAN_RESULT_TREE_MAX_AGE_DAYS`` (default 14 days) are reaped with
  a per-tree receipt appended to ``reaped_generation_trees.jsonl`` (receipt
  first: a tree whose receipt cannot be written is kept). Live, published,
  abandoned-ledger and strict-invocation-bearing trees are never touched.
- **Cost-ledger compaction.** ``generation_cost_ledger.jsonl`` keeps the
  last ``POK_COST_LEDGER_RETENTION_DAYS`` (default 30) days of full rows
  verbatim; older rows are compacted — under the exact
  ``_locked_state_sidecar`` exclusive sidecar lock every ledger reader and
  writer already takes — into per-month summary rows, with a per-row
  summary receipt (line digest + accounting scalars) under
  ``cost_ledger_compaction_receipts/`` so no row silently disappears. The
  receipt is written before the ledger rewrite; a crash between the two
  only duplicates receipt accounting, never loses a row.

Governance additions (2026-10-21, post-review R1-R5):

- **Janitor safe window** (``janitor_safe_window``): the single testable
  predicate gating the destructive governance steps. A: no active/blocked
  post-publication handoff claim. B: generation-gap or pre-Master stage
  (``selected``/``prepared``/``crossover_running``/``direction_audited``),
  never an unparseable checkpoint, never with a live-ahead draft slot. C:
  disk-pressure tiers (normal/tight/critical) that only *widen* which steps
  may run — A/B always dominate; closed means closed.
- **Identity keep knob** ``POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS`` (G1
  pattern): tightens the retention window at the janitor call site without
  touching ``evaluation_identity_retention`` (its library default stays 20).
- **Retired-identity purge**: physically removes staged
  ``archive/evaluation_identity_retired/`` snapshots that a retention
  receipt moved at least ``POK_IDENTITY_RETIRED_PURGE_AFTER_DAYS`` (7) days
  ago and that a freshly re-run
  ``live_evaluation_identity_references`` proves still unreferenced.
  Fail-closed on ANY reference-check exception; receipt-first via a
  self-digesting ``evaluation_identity_retired_purged`` receipt.
  ``retention_receipts/`` is never touched.
- **Aged abandon-transaction reap**: removes
  ``policy_epoch_abandon_transactions/<64hex>/`` trees only when the
  receipt proves ``checkpoint_cleared`` + ``candidate_state == quarantine``,
  the claimed ``next_v`` is strictly below
  ``max(live next_v, published high-water, find_abandoned_version_floor())``
  and the tree is older than ``POK_ABANDON_TXN_RETENTION_DAYS`` (30). Skips
  the whole round while ``policy_epoch_reconciliation_claim.json`` exists
  (an active claim re-proves those claim bytes byte-for-byte). Receipt-first
  into ``reaped_abandon_transactions.jsonl``; the abandon ledger itself is
  never read-for-write here and version re-allocation keeps reading the
  ledger (``abandoned_version_attempt_count``), not these directories.

It runs from the app lifespan beside the LLM saturator so it keeps working
even when the orchestrator is mid-call. Failures are logged and swallowed.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("pok.disk")

_YES = {"1", "true", "yes", "on"}

_VERSION_TOKEN = re.compile(r"v(\d+)", re.IGNORECASE)

# Live checkpoints and ledgers the janitor must never unlink.
_PROTECTED_RESULT_NAMES = frozenset({
    "pipeline_state.json",
    "abandoned_versions.jsonl",
    "abandoned_versions.jsonl.lock",
    "evaluation_cycle_manifest.json",
    "glicko_ratings.json",
    "head_to_head.json",
    "bot_stats.json",
    "selection_snapshot.json",
    "elo_daemon_stats.json",
    "match_history.jsonl",
    "rating_history.jsonl",
    "events.jsonl",
    "llm_costs.jsonl",
    "worker_failures.jsonl",
    "reaped_bots.jsonl",
    "rate_limit_state.json",
    "llm_availability_pause.json",
    "generation_cost_pending.json",
    "reaped_generation_trees.jsonl",
    "reaped_replays.jsonl",
    "reaped_abandon_transactions.jsonl",
    "policy_epoch_reset_receipt.json",
    "policy_epoch_reconciliation_receipt.json",
    "policy_epoch_reconciliation_claim.json",
})

DEFAULT_MIN_FREE_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_INTERVAL_SEC = 300.0
DEFAULT_KEEP_SATURATOR_SESSIONS = 80
DEFAULT_KEEP_SATURATOR_SESSIONS_PRESSURE = 20
DEFAULT_KEEP_ABANDONED_RESULT_DIRS = 8
DEFAULT_KEEP_ABANDONED_RESULT_DIRS_PRESSURE = 3
DEFAULT_KEEP_FINDINGS_LINES = 400
DEFAULT_KEEP_METRICS_LINES = 8000
DEFAULT_KEEP_ORCHESTRATOR_LOGS = 20

# T7a: orphan never-published ``results/vN`` trees.
DEFAULT_ORPHAN_RESULT_TREE_MAX_AGE_DAYS = 14.0

# T7b: bounded retention for the generation cost ledger.
COST_LEDGER_NAME = "generation_cost_ledger.jsonl"
COST_LEDGER_RECEIPTS_DIR_NAME = "cost_ledger_compaction_receipts"
COST_LEDGER_SUMMARY_KIND = "cost_ledger_monthly_summary"
DEFAULT_COST_LEDGER_RETENTION_DAYS = 30.0
DEFAULT_COST_LEDGER_COMPACTION_MIN_INTERVAL_SEC = 6.0 * 3600.0
REAPED_TREES_LEDGER_NAME = "reaped_generation_trees.jsonl"

# G1 (2026-10-20): the janitor's candidates-ledger call used the hard-coded
# library default (``candidate_store.DEFAULT_KEEP_VERSIONS`` = 50), so the
# only production knob was a code change. ``POK_CANDIDATE_LEDGER_KEEP_VERSIONS``
# tightens the retention window at the janitor call site — behaviorally
# identical to re-defaulting ``compact_candidate_ledger`` itself, without
# touching that module. Values < 1 clamp to 1; unparsable values fall back to
# the library default; unset forwards the library default explicitly so the
# compaction receipt always records the effective window.
CANDIDATE_LEDGER_KEEP_VERSIONS_ENV = "POK_CANDIDATE_LEDGER_KEEP_VERSIONS"

# --- Governance (2026-10-21) -------------------------------------------------
# Disk-pressure tiers for the janitor safe window. ``tight`` (< 6 GiB free)
# enables the retired-identity purge and abandon-transaction reap at their
# default windows; ``critical`` (< POK_DISK_MIN_FREE_GB, default 4 GiB, the
# same threshold the loop already uses for retention pressure) tightens them
# (retired 1 day / transactions 7 days) and caps the identity keep window at
# 4. Tiers only widen what may run; the A/B window rules always dominate.
DEFAULT_DISK_TIGHT_FREE_GB = 6.0
DISK_TIGHT_FREE_GB_ENV = "POK_DISK_TIGHT_FREE_GB"

# G1-pattern knob for the evaluation-identity retention window: identical in
# spirit to POK_CANDIDATE_LEDGER_KEEP_VERSIONS — the janitor call site
# tightens the window without touching evaluation_identity_retention (the
# library default KEEP_RECENT_SNAPSHOTS = 20 stays authoritative for every
# other caller). Values < 1 clamp to 1; unparsable values fall back to the
# library default.
IDENTITY_KEEP_ENV = "POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS"
IDENTITY_KEEP_CRITICAL_CAP = 4

# Retired-identity purge + abandon-transaction reap windows (days).
RETIRED_PURGE_DAYS_ENV = "POK_IDENTITY_RETIRED_PURGE_AFTER_DAYS"
DEFAULT_RETIRED_PURGE_DAYS = 7.0
RETIRED_PURGE_CRITICAL_DAYS = 1.0
ABANDON_TXN_DAYS_ENV = "POK_ABANDON_TXN_RETENTION_DAYS"
DEFAULT_ABANDON_TXN_DAYS = 30.0
ABANDON_TXN_CRITICAL_DAYS = 7.0

# Events auto-compression (A-track entry, frozen signature): default OFF so
# the production step is inert until the operator explicitly enables it AND
# the disk tier is critical AND the safe window is open. When enabled, the
# entry gzips the cold prefix and re-heads events.jsonl after digest proof.
EVENTS_ROTATION_ENV = "POK_EVENTS_AUTO_ROTATION_ENABLED"
EVENTS_ROTATION_ACTOR = "disk_hygiene_janitor"

# Receipt ledgers for the governance steps (receipt-first deletions).
REAPED_ABANDON_TXN_LEDGER_NAME = "reaped_abandon_transactions.jsonl"

# Pre-Master checkpoint stages at which no strict-invocation / consumer-gate
# evidence is being produced and a generation may be safely quiesced. Mirrors
# STAGE_ORDER in pipeline_state.py (keep in sync when stages change):
# ``preparing`` (materializing) and ``master_planned`` onwards are excluded.
JANITOR_OPEN_STAGES = frozenset(
    {"selected", "prepared", "crossover_running", "direction_audited"}
)


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in _YES


def _version_token(name: str) -> int | None:
    match = _VERSION_TOKEN.search(str(name or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _path_size(path: Path) -> int:
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return int(path.stat().st_size)
        if not path.is_dir():
            return 0
    except OSError:
        return 0
    total = 0
    try:
        for root, dirnames, filenames in os.walk(path, followlinks=False):
            root_path = Path(root)
            # Do not descend through symlinks.
            dirnames[:] = [
                name
                for name in dirnames
                if not (root_path / name).is_symlink()
            ]
            for name in filenames:
                file_path = root_path / name
                try:
                    if file_path.is_symlink():
                        continue
                    total += int(file_path.stat().st_size)
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _rm(path: Path) -> int:
    """Unlink a file or tree. Returns bytes roughly freed. Never follows links."""
    try:
        if path.is_symlink() or not path.exists():
            return 0
    except OSError:
        return 0
    size = _path_size(path)
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
    except OSError:
        return 0
    try:
        gone = not path.exists()
    except OSError:
        gone = False
    return size if gone else 0


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _checkpoint_versions(payload: dict[str, Any]) -> set[int]:
    found: set[int] = set()
    for key in ("next_v", "source_v", "parent2_v"):
        try:
            value = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            found.add(value)
    binding = payload.get("epoch_binding")
    if isinstance(binding, dict):
        try:
            high = int(binding.get("published_high_water") or 0)
        except (TypeError, ValueError):
            high = 0
        if high > 0:
            found.add(high)
        parents = binding.get("parent_versions")
        if isinstance(parents, list):
            for item in parents:
                try:
                    version = int(item)
                except (TypeError, ValueError):
                    continue
                if version > 0:
                    found.add(version)
    return found


def live_protected_versions(results_dir: Path) -> set[int]:
    """Versions named by the primary checkpoint and live-ahead drafts.

    Stale consumer/draft checkpoint files must not protect themselves: a
    leftover ``pipeline_state_consumer-candidate-v176.json`` would otherwise
    pin ``results/v176`` and the consumer file forever.
    """
    live: set[int] = set()
    payload = _load_json(results_dir / "pipeline_state.json")
    if payload:
        live.update(_checkpoint_versions(payload))
    high = 0
    if payload:
        binding = payload.get("epoch_binding")
        if isinstance(binding, dict):
            try:
                high = int(binding.get("published_high_water") or 0)
            except (TypeError, ValueError):
                high = 0
    for path in results_dir.glob("pipeline_state_draft*.json"):
        draft = _load_json(path)
        if not draft:
            continue
        try:
            next_v = int(draft.get("next_v") or 0)
        except (TypeError, ValueError):
            next_v = 0
        if high and next_v and next_v <= high:
            continue
        live.update(_checkpoint_versions(draft))
    return live


def published_bot_versions(bots_dir: Path | None) -> set[int]:
    versions: set[int] = set()
    if bots_dir is None or not bots_dir.is_dir():
        return versions
    try:
        children = list(bots_dir.iterdir())
    except OSError:
        return versions
    for child in children:
        try:
            if not child.is_dir() or child.is_symlink():
                continue
            if not (child / ".completed").is_file():
                continue
        except OSError:
            continue
        version = _version_token(child.name)
        if version:
            versions.add(version)
    return versions


def abandoned_versions(results_dir: Path) -> list[int]:
    path = results_dir / "abandoned_versions.jsonl"
    versions: list[int] = []
    try:
        if not path.is_file() or path.is_symlink():
            return versions
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                try:
                    version = int(row.get("version") or 0)
                except (TypeError, ValueError):
                    continue
                if version > 0:
                    versions.append(version)
    except OSError:
        return versions
    return versions


def disk_free_bytes(path: Path) -> int | None:
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def _trim_jsonl(path: Path, keep_lines: int) -> int:
    if keep_lines < 0 or not path.is_file() or path.is_symlink():
        return 0
    try:
        raw = path.read_bytes()
    except OSError:
        return 0
    if not raw:
        return 0
    lines = raw.splitlines(keepends=True)
    if len(lines) <= keep_lines:
        return 0
    kept = b"".join(lines[-keep_lines:])
    tmp = path.with_name(path.name + ".hygiene.tmp")
    try:
        tmp.write_bytes(kept)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return 0
    return max(0, len(raw) - len(kept))


def _prune_saturator(
    results_dir: Path,
    *,
    keep_sessions: int,
    keep_findings_lines: int,
) -> tuple[int, int]:
    freed = 0
    removed = 0
    saturator_dir = results_dir / "saturator"
    session_sizes: dict[str, int] = {}
    if saturator_dir.is_dir() and not saturator_dir.is_symlink():
        for path in saturator_dir.glob("session_*.txt"):
            session_sizes[path.name] = _path_size(path)
    try:
        from llm_saturator import _housekeep_session_files

        _housekeep_session_files(saturator_dir, keep_sessions=keep_sessions)
    except Exception as exc:
        log.warning("saturator session prune failed: %s", exc)
    for name, size in session_sizes.items():
        leftover = saturator_dir / name
        try:
            still = leftover.exists()
        except OSError:
            still = True
        if not still:
            freed += size
            removed += 1
    findings = saturator_dir / "findings.jsonl"
    trimmed = _trim_jsonl(findings, keep_findings_lines)
    if trimmed:
        freed += trimmed
        removed += 1
    return freed, removed


def _live_draft_slot_names(results_dir: Path, published: set[int]) -> set[str]:
    """Draft slot directory names that still have a live-ahead checkpoint."""
    protected: set[str] = set()
    for path in results_dir.glob("pipeline_state_draft*.json"):
        if path.name.endswith(".lock"):
            continue
        payload = _load_json(path)
        if not payload:
            continue
        try:
            next_v = int(payload.get("next_v") or 0)
        except (TypeError, ValueError):
            next_v = 0
        # A draft at or behind the published high-water cannot promote.
        if published and next_v and next_v <= max(published):
            continue
        slot = path.stem.removeprefix("pipeline_state_")
        protected.add(slot)
        if next_v:
            protected.add(f"national_cloud_v{next_v}")
    return protected


def _reap_orphan_drafts(
    results_dir: Path,
    *,
    published: set[int],
    orphan_max_age_sec: float = 3600.0,
) -> tuple[int, int]:
    freed = 0
    removed = 0
    live_slots = _live_draft_slot_names(results_dir, published)
    reaped_slots: set[str] = set()
    # Drop stale draft *checkpoint files* that sit at or behind high-water.
    for path in list(results_dir.glob("pipeline_state_draft*.json")):
        if path.name.endswith(".lock"):
            continue
        payload = _load_json(path)
        if not payload:
            continue
        try:
            next_v = int(payload.get("next_v") or 0)
        except (TypeError, ValueError):
            next_v = 0
        if published and next_v and next_v <= max(published):
            slot = path.stem.removeprefix("pipeline_state_")
            reaped_slots.add(slot)
            if next_v:
                reaped_slots.add(f"national_cloud_v{next_v}")
            freed += _rm(path)
            lock = path.with_name(path.name + ".lock")
            freed += _rm(lock)
            removed += 1
    draft_root = results_dir / "draft_candidates"
    if not draft_root.is_dir() or draft_root.is_symlink():
        return freed, removed
    try:
        children = list(draft_root.iterdir())
    except OSError:
        return freed, removed
    now = time.time()
    for child in children:
        try:
            if child.is_symlink():
                continue
        except OSError:
            continue
        if child.name in live_slots:
            continue
        version = _version_token(child.name)
        if version and version in published:
            freed += _rm(child)
            removed += 1
            continue
        if child.name in reaped_slots:
            freed += _rm(child)
            removed += 1
            continue
        # Unnamed leftovers (no live-ahead checkpoint) wait an hour so a
        # draft directory created before its checkpoint is not reaped.
        try:
            age = now - child.stat().st_mtime
        except OSError:
            continue
        if age < orphan_max_age_sec:
            continue
        freed += _rm(child)
        removed += 1
    return freed, removed


def _reap_stale_consumer_checkpoints(
    results_dir: Path,
    *,
    live: set[int],
    published: set[int],
) -> tuple[int, int]:
    freed = 0
    removed = 0
    high = max(published) if published else 0
    for path in list(results_dir.glob("pipeline_state_consumer-candidate-*.json")):
        version = _version_token(path.stem)
        if version is None:
            continue
        if version in live:
            continue
        if high and version <= high:
            freed += _rm(path)
            freed += _rm(path.with_name(path.name + ".lock"))
            removed += 1
    return freed, removed


def _tree_contains_strict_invocations(tree: Path) -> bool:
    """True when any ``strict_invocations`` entry exists inside the tree.

    Fail closed: an unreadable tree reports True (never reaped by the orphan
    branch). Symlinked entries count as protective without being followed.
    """
    canonical = tree / "logs" / "strict_invocations"
    if os.path.lexists(canonical):
        return True
    errored = False

    def _note(_error) -> None:
        nonlocal errored
        errored = True

    for _root, dirnames, _filenames in os.walk(tree, followlinks=False, onerror=_note):
        if "strict_invocations" in dirnames:
            return True
    return errored


def _consumer_checkpoint_versions(results_dir: Path) -> set[int]:
    """Versions named by a still-present consumer checkpoint file."""
    versions: set[int] = set()
    for path in results_dir.glob("pipeline_state_consumer-candidate-v*.json"):
        version = _version_token(path.stem)
        if version:
            versions.add(version)
    return versions


def _slice2b_referenced_versions(results_dir: Path) -> set[int] | None:
    """Versions pinned by non-terminal Slice-2b lifecycle candidates.

    Returns ``None`` when the lifecycle database is present but unreadable —
    the caller then fails closed and reaps no orphan tree this pass.
    """
    db_path = results_dir / "workflow" / "slice2b_lifecycle.sqlite3"
    if not db_path.is_file() or db_path.is_symlink():
        return set()
    import sqlite3

    try:
        connection = sqlite3.connect(db_path, timeout=30.0)
        try:
            rows = connection.execute(
                "SELECT candidate_id FROM slice2b_candidate_lifecycle "
                "WHERE state NOT IN ('promoted', 'rejected')"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    versions: set[int] = set()
    for row in rows:
        version = _version_token(row[0] if row else "")
        if version:
            versions.add(version)
    return versions


def _orphan_reference_versions(results_dir: Path, live: set[int]) -> set[int] | None:
    """Every live-reference surface for the orphan branch, or None (unreadable).

    Fail-closed on a present-but-unparsable primary checkpoint: the live set
    derived upstream is unreliable then, so the orphan branch must reap
    nothing this pass (red-team probe 2026-10-15 — a corrupt
    pipeline_state.json silently emptied the live set and made the live
    generation's own tree orphan-reapable).
    """
    primary = results_dir / "pipeline_state.json"
    if primary.exists() and _load_json(primary) is None:
        return None
    references = set(live) | _consumer_checkpoint_versions(results_dir)
    lifecycle = _slice2b_referenced_versions(results_dir)
    if lifecycle is None:
        return None
    references |= lifecycle
    return references


def _candidate_ledger_keep_versions() -> int:
    """Resolve the janitor's candidates-ledger window from the environment.

    Single source of truth shared with the operator one-shot entry
    ``scripts/compact_candidate_ledger.py`` so the janitor cadence and a
    manual tighten can never disagree about what a window value means.
    """
    try:
        from candidate_store import DEFAULT_KEEP_VERSIONS
    except Exception:  # pragma: no cover - import guard mirrors the step
        DEFAULT_KEEP_VERSIONS = 50
    raw = os.environ.get(CANDIDATE_LEDGER_KEEP_VERSIONS_ENV)
    if raw is None or not str(raw).strip():
        return int(DEFAULT_KEEP_VERSIONS)
    try:
        window = int(str(raw).strip())
    except (TypeError, ValueError):
        return int(DEFAULT_KEEP_VERSIONS)
    return max(1, window)


def _env_gib(name: str, default_gb: float) -> int:
    """Parse a GiB-denominated env threshold the way the hygiene loop does."""
    try:
        value = float(os.environ.get(name, str(default_gb)))
    except (TypeError, ValueError):
        value = default_gb
    return int(max(0.5, value) * 1024 * 1024 * 1024)


def _identity_keep_snapshots() -> int:
    """Resolve the janitor's identity-snapshot window from the environment.

    G1 pattern (mirrors ``_candidate_ledger_keep_versions``): the knob lives
    at the janitor call site, the library default
    (``evaluation_identity_retention.KEEP_RECENT_SNAPSHOTS`` = 20) stays
    authoritative for the migration writer. Values < 1 clamp to 1; unparsable
    or unset values fall back to the library default.
    """
    try:
        from evaluation_identity_retention import KEEP_RECENT_SNAPSHOTS
    except Exception:  # pragma: no cover - import guard mirrors the step
        KEEP_RECENT_SNAPSHOTS = 20
    raw = os.environ.get(IDENTITY_KEEP_ENV)
    if raw is None or not str(raw).strip():
        return int(KEEP_RECENT_SNAPSHOTS)
    try:
        keep = int(str(raw).strip())
    except (TypeError, ValueError):
        return int(KEEP_RECENT_SNAPSHOTS)
    return max(1, keep)


# Sentinel distinguishing "caller did not pass free_bytes" (measure it) from
# an explicit None ("unmeasurable" -> tier unknown, never a destructive tier).
_UNMEASURED: Any = object()


def janitor_safe_window(
    results_dir: Path,
    *,
    published: set[int] | None = None,
    free_bytes: Any = _UNMEASURED,
    tight_free_bytes: int | None = None,
    critical_free_bytes: int | None = None,
) -> dict[str, Any]:
    """Decide whether the destructive governance steps may run this cycle.

    One independently testable predicate (the operator's safe-window rule):

    A. **No active handoff claim.** ``pending_handoff_route()["status"]``
       must be ``"none"``. ``"pending"`` (including running / awaiting
       takeover) and ``"blocked"`` (unreadable = unprovable) both close the
       window — fail closed, mirroring the discover posture.
    B. **Stage gate.** A missing primary checkpoint (generation gap) opens
       the window. A parseable checkpoint opens it only at
       {selected, prepared, crossover_running, direction_audited}
       (JANITOR_OPEN_STAGES; ``preparing`` is materializing, ``master_planned``
       onwards means active LLM / strict-log / consumer gates). An
       unparseable-but-present checkpoint closes it (same fail-closed posture
       as ``_orphan_reference_versions``). Any live-ahead draft slot
       (``_live_draft_slot_names``) — or any unparseable draft checkpoint —
       closes it: a draft behind the promotion barrier can publish and freeze
       rotation plans without passing ``direction_audited`` again.
    C. **Disk-pressure tier.** normal (>= POK_DISK_TIGHT_FREE_GB, default
       6 GiB) / tight (< 6 GiB) / critical (< POK_DISK_MIN_FREE_GB, default
       4 GiB — the loop's existing pressure threshold). Tight enables the
       retired purge (7 d) and transaction reap (30 d); critical tightens
       them (1 d / 7 d), caps identity keep at 4 and is the only tier where
       events auto-rotation may run (still behind its own env flag).
       Unmeasurable free space maps to ``"unknown"`` and never unlocks a
       destructive tier (same posture as the pressure flag).

    A/B always dominate: with any closed reason the window is closed at
    every tier — tiers only widen what may run, they never weaken A/B.
    """
    results_dir = Path(results_dir)
    closed: list[str] = []

    # A — active post-publication handoff.
    try:
        from post_publication_handoff import pending_handoff_route

        handoff_status = str(pending_handoff_route().get("status") or "")
    except Exception:
        handoff_status = "blocked"
    if handoff_status != "none":
        closed.append(f"handoff_{handoff_status or 'unknown'}")

    # B — stage gate.
    primary = results_dir / "pipeline_state.json"
    payload: dict[str, Any] | None = None
    if primary.exists():
        payload = _load_json(primary)
        if payload is None:
            closed.append("primary_checkpoint_unreadable")
        else:
            stage = str(payload.get("stage") or "")
            if stage not in JANITOR_OPEN_STAGES:
                closed.append(f"stage_{stage or 'unknown'}")
    published_set = set(published or set())
    if payload:
        binding = payload.get("epoch_binding")
        if isinstance(binding, dict):
            try:
                binding_high = int(binding.get("published_high_water") or 0)
            except (TypeError, ValueError):
                binding_high = 0
            if binding_high > 0:
                published_set.add(binding_high)
    if _live_draft_slot_names(results_dir, published_set):
        closed.append("live_ahead_draft")
    for draft_path in results_dir.glob("pipeline_state_draft*.json"):
        if draft_path.name.endswith(".lock"):
            continue
        if _load_json(draft_path) is None:
            closed.append("draft_checkpoint_unreadable")
            break

    # C — disk-pressure tier.
    if free_bytes is _UNMEASURED:
        free_bytes = disk_free_bytes(results_dir)
    tight = (
        _env_gib(DISK_TIGHT_FREE_GB_ENV, DEFAULT_DISK_TIGHT_FREE_GB)
        if tight_free_bytes is None
        else int(tight_free_bytes)
    )
    critical = (
        _env_gib("POK_DISK_MIN_FREE_GB", 4.0)
        if critical_free_bytes is None
        else int(critical_free_bytes)
    )
    if free_bytes is None:
        tier = "unknown"
    elif int(free_bytes) < critical:
        tier = "critical"
    elif int(free_bytes) < tight:
        tier = "tight"
    else:
        tier = "normal"

    eligible = tier in ("tight", "critical")
    return {
        "open": not closed,
        "closed_reasons": closed,
        "tier": tier,
        "free_bytes": free_bytes,
        "retired_purge_days": (
            (
                RETIRED_PURGE_CRITICAL_DAYS
                if tier == "critical"
                else _env_float(RETIRED_PURGE_DAYS_ENV, DEFAULT_RETIRED_PURGE_DAYS)
            )
            if eligible
            else None
        ),
        "abandon_txn_days": (
            (
                ABANDON_TXN_CRITICAL_DAYS
                if tier == "critical"
                else _env_float(ABANDON_TXN_DAYS_ENV, DEFAULT_ABANDON_TXN_DAYS)
            )
            if eligible
            else None
        ),
        "identity_keep_cap": (
            IDENTITY_KEEP_CRITICAL_CAP if tier == "critical" else None
        ),
        "events_rotation_tier_allowed": tier == "critical",
    }


def _emit_governance_event(
    event_type: str, severity: str, message: str, data: dict[str, Any]
) -> None:
    try:
        from system_log import log_system_event

        log_system_event(event_type, severity, message, data)
    except Exception:  # pragma: no cover - event emission is best-effort
        pass


def _append_locked_jsonl_receipt(
    results_dir: Path, ledger_name: str, row: dict[str, Any]
) -> None:
    """Durably append one governance receipt row under the sidecar lock."""
    path = Path(results_dir) / ledger_name
    payload = json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        from evolution_infra import locked_file
    except Exception:
        locked_file = None
    handle: Any
    try:
        if locked_file is not None:
            with locked_file(path, "a+", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
    except OSError as exc:
        raise RuntimeError(f"{ledger_name} receipt append failed: {exc}") from exc


def _compact_candidate_ledger_step(results_dir: Path) -> dict[str, Any] | None:
    """Run the sidecar-locked candidates-ledger retention window (T3 wiring).

    ``compact_candidate_ledger`` shipped as library code with no production
    caller (red-team finding 2026-10-15); the janitor owns its cadence here.
    It holds the append sidecar lock, rewrites only when there is something
    to compact, and a no-op run touches nothing on disk. The window comes
    from ``POK_CANDIDATE_LEDGER_KEEP_VERSIONS`` (G1, 2026-10-20), clamped to
    >= 1, defaulting to the library's 50.
    """
    ledger = results_dir / "candidates.jsonl"
    if not ledger.is_file():
        return None
    try:
        from candidate_store import compact_candidate_ledger
    except Exception as exc:  # pragma: no cover - import guard
        log.warning("candidate ledger compaction unavailable: %s", exc)
        return None
    try:
        return compact_candidate_ledger(
            ledger, keep_versions=_candidate_ledger_keep_versions()
        )
    except Exception as exc:
        log.warning("candidate ledger compaction failed: %s", exc)
        return None


def _evaluation_identity_retention_step(
    results_dir: Path, *, window: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """T6 wiring on a non-identity carrier: keep the newest identity snapshots.

    ``evaluation_data_identity.py`` and ``elo_daemon.py`` both sit inside the
    rating SEMANTIC_PATHS identity set, so the retention hook must not live in
    either. The janitor owns the cadence here instead, holding the same
    exclusive cycle + manifest locks the migration writer holds. A lock-free
    pre-check skips the pass entirely while the archive is within budget, so
    the steady-state janitor cycle never touches those locks.

    The window comes from ``POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS`` (G1,
    2026-10-21): clamp >= 1, unparsable/unset -> library default 20, never
    touching ``evaluation_identity_retention`` itself. The critical disk tier
    additionally caps the effective keep at
    ``IDENTITY_KEEP_CRITICAL_CAP`` (4).
    """
    from evaluation_identity_retention import KEEP_RECENT_SNAPSHOTS  # noqa: F401

    keep = _identity_keep_snapshots()
    cap = (window or {}).get("identity_keep_cap")
    if cap:
        keep = min(keep, int(cap))
    archive_dir = results_dir / "archive" / "evaluation_identity"
    if not archive_dir.is_dir():
        return None
    try:
        snapshots = [
            entry
            for entry in archive_dir.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        ]
    except OSError:
        return None
    if len(snapshots) <= keep:
        return None
    try:
        from evaluation_bundle import evaluation_cycle_lock
        from evaluation_data_identity import _manifest_lock
        from evaluation_identity_retention import run_evaluation_identity_retention
    except Exception as exc:  # pragma: no cover - import guard
        log.warning("evaluation identity retention unavailable: %s", exc)
        return None
    try:
        with evaluation_cycle_lock(results_dir, exclusive=True), _manifest_lock(
            results_dir
        ):
            return run_evaluation_identity_retention(
                results_dir,
                triggered_by="disk_hygiene_janitor",
                keep=keep,
            )
    except Exception as exc:
        log.warning("evaluation identity retention failed: %s", exc)
        return None


def _purge_retired_identity_snapshots(
    results_dir: Path, *, window: dict[str, Any]
) -> dict[str, Any] | None:
    """Physically purge aged retired evaluation-identity snapshots.

    The retirement staging area (``archive/evaluation_identity_retired/``)
    is operator-purgeable by design (its module docstring says so); this
    automates that purge under four conditions, all of which must hold:

    - the janitor safe window is open and the disk tier is tight/critical
      (the window's ``retired_purge_days`` carries the effective age, 7 d
      tight / 1 d critical);
    - the snapshot directory matches ``SNAPSHOT_NAME_RE`` AND appears in the
      ``moved[]`` list of a retention receipt whose ``created_at`` is older
      than the window (a receipt that cannot be read protects the entry by
      omission);
    - a freshly re-run ``live_evaluation_identity_references`` proves its
      archived ``manifest_digest`` is still unreferenced. ANY exception from
      that check fails the WHOLE round closed (review R2: never catch broad
      and continue with an empty reference set — that would delete a
      still-referenced snapshot);
    - receipt-first: one self-digesting ``evaluation_identity_retired_purged``
      receipt (name/bytes/manifest digest/source receipt digest) is durably
      written before the first ``rmtree``; a receipt failure keeps everything.

    ``retention_receipts/`` itself is never purged (it never matches
    ``SNAPSHOT_NAME_RE`` and is skipped explicitly).
    """
    if not window.get("open"):
        return {"skipped": "window_closed"}
    days = window.get("retired_purge_days")
    if days is None:
        return {"skipped": "tier_not_eligible"}
    retired = results_dir / "archive" / "evaluation_identity_retired"
    if not retired.is_dir() or retired.is_symlink():
        return None
    try:
        from evaluation_identity_retention import (
            RECEIPTS_DIRNAME,
            SNAPSHOT_NAME_RE,
            _snapshot_manifest_digest,
            _write_receipt,
            live_evaluation_identity_references,
        )
    except Exception as exc:  # pragma: no cover - import guard
        log.warning("retired identity purge unavailable: %s", exc)
        return {"skipped": f"import_failed:{type(exc).__name__}"}

    receipts_dir = retired / RECEIPTS_DIRNAME
    moved_at: dict[str, tuple[float, str, str]] = {}
    if receipts_dir.is_dir() and not receipts_dir.is_symlink():
        try:
            receipt_files = sorted(receipts_dir.iterdir())
        except OSError:
            receipt_files = []
        for receipt_path in receipt_files:
            try:
                if (
                    not receipt_path.is_file()
                    or receipt_path.is_symlink()
                    or receipt_path.suffix != ".json"
                ):
                    continue
            except OSError:
                continue
            body = _load_json(receipt_path)
            if not body:
                continue
            created = _as_float(body.get("created_at"))
            if created <= 0:
                continue
            for item in body.get("moved") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if not SNAPSHOT_NAME_RE.fullmatch(name):
                    continue
                previous = moved_at.get(name)
                if previous is None or created > previous[0]:
                    moved_at[name] = (
                        created,
                        str(body.get("receipt_digest") or ""),
                        receipt_path.name,
                    )

    # R2 fail-closed: an unreadable live reference surface aborts the whole
    # round — it may be the one file that pins a still-live identity.
    try:
        live_references = live_evaluation_identity_references(results_dir)
    except Exception as exc:
        log.warning(
            "retired identity purge skipped (live reference check failed): %s",
            exc,
        )
        return {"skipped": f"live_reference_check_failed:{type(exc).__name__}"}

    cutoff = time.time() - float(days) * 24.0 * 3600.0
    targets: list[tuple[Path, str | None, tuple[float, str, str], int]] = []
    try:
        entries = sorted(retired.iterdir())
    except OSError:
        return {"purged": [], "bytes_freed": 0}
    for entry in entries:
        if entry.name == RECEIPTS_DIRNAME:
            continue
        if not SNAPSHOT_NAME_RE.fullmatch(entry.name):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except OSError:
            continue
        record = moved_at.get(entry.name)
        if record is None:
            continue  # never proven moved by a retention receipt
        created, _source_digest, _source_name = record
        if created > cutoff:
            continue  # staged too recently
        try:
            digest = _snapshot_manifest_digest(entry)
        except Exception:
            continue  # unreadable archived manifest protects the snapshot
        if digest is None or digest in live_references:
            continue  # unprovable or still live-referenced
        targets.append((entry, digest, record, _path_size(entry)))
    if not targets:
        return {"purged": [], "bytes_freed": 0}

    receipt = {
        "schema_version": 1,
        "kind": "evaluation_identity_retired_purged",
        "created_at": time.time(),
        "triggered_by": "disk_hygiene_janitor",
        "retention_days": float(days),
        "results_dir": str(results_dir),
        "purged": [
            {
                "name": entry.name,
                "manifest_digest": digest,
                "bytes": size,
                "moved_by_receipt": {
                    "name": record[2],
                    "receipt_digest": record[1],
                    "created_at": record[0],
                },
            }
            for entry, digest, record, size in targets
        ],
        "bytes_total": sum(size for _e, _d, _r, size in targets),
        "live_reference_count": len(live_references),
    }
    try:
        _write_receipt(receipts_dir, receipt)
    except Exception as exc:
        log.warning(
            "retired identity purge kept everything (receipt failed): %s", exc
        )
        return {"skipped": f"receipt_failed:{type(exc).__name__}"}

    freed = 0
    purged: list[str] = []
    for entry, _digest, _record, _size in targets:
        gone = _rm(entry)
        if gone:
            purged.append(entry.name)
            freed += gone
    return {
        "purged": purged,
        "bytes_freed": freed,
        "receipt_kind": "evaluation_identity_retired_purged",
    }


def _reap_aged_abandon_transactions(
    results_dir: Path, *, window: dict[str, Any], published_high: int = 0
) -> dict[str, Any] | None:
    """Reap receipt-complete, aged abandon transaction quarantine trees.

    ``policy_epoch_abandon_transactions/<64hex>/`` directories hold the
    quarantined candidate + claim/receipt of publication-linearized
    abandons. They are safe to remove only when ALL of these hold:

    - the janitor safe window is open and the tier is tight/critical, with
      the window's ``abandon_txn_days`` (30 d tight / 7 d critical);
    - no ``policy_epoch_reconciliation_claim.json`` exists (review R3 ii):
      an active epoch-reconciliation claim re-proves transaction claim
      bytes byte-for-byte, and a crash between receipt-write and claim-clear
      leaves exactly a receipt-complete transaction under a live claim;
    - ``receipt.json`` proves ``checkpoint_cleared is True`` and
      ``candidate_state == "quarantine"`` (transactions in ``absent`` /
      ``source`` states are NEVER reaped — deliberate conservatism, R3 iv);
    - the claim's ``checkpoint.next_v`` is strictly below
      ``max(live next_v, published high-water, find_abandoned_version_floor())``
      — the version label is fully re-allocatable via the ledger, whose
      attempt counter (``abandoned_version_attempt_count``) reads the
      ledger rows, not these directories (verified read-only);
    - the tree is older than the window (max of directory and receipt
      mtimes).

    Known cost (R3 i, accepted): ``_finalized_canonical_abandon``
    (bootstrap_contract_recovery_historical.py) scans every transaction
    directory to re-prove a vanished checkpoint as a completed abandon;
    after a purge that workflow's recovery is permanently unprovable. The
    30-day default dwarfs the 4-hour cycle timeout, and the durable abandon
    ledger row (never touched here) remains the canonical record.

    Receipt-first: each reap appends to ``reaped_abandon_transactions.jsonl``
    before the ``rmtree``; a ledger-append failure keeps the tree. An
    unreadable floor input (primary checkpoint, ledger floor) skips the
    whole round — fail closed, mirroring ``_orphan_reference_versions``.
    """
    if not window.get("open"):
        return {"skipped": "window_closed"}
    days = window.get("abandon_txn_days")
    if days is None:
        return {"skipped": "tier_not_eligible"}
    if (results_dir / "policy_epoch_reconciliation_claim.json").exists():
        return {"skipped": "reconciliation_claim_active"}
    root = results_dir / "policy_epoch_abandon_transactions"
    if not root.is_dir() or root.is_symlink():
        return None

    live_next = 0
    binding_high = 0
    primary = results_dir / "pipeline_state.json"
    if primary.exists():
        payload = _load_json(primary)
        if payload is None:
            return {"skipped": "primary_checkpoint_unreadable"}
        try:
            live_next = int(payload.get("next_v") or 0)
        except (TypeError, ValueError):
            live_next = 0
        binding = payload.get("epoch_binding")
        if isinstance(binding, dict):
            try:
                binding_high = int(binding.get("published_high_water") or 0)
            except (TypeError, ValueError):
                binding_high = 0
    try:
        from abandoned_version_ledger import find_abandoned_version_floor

        ledger_floor = int(find_abandoned_version_floor())
    except Exception as exc:
        log.warning(
            "abandon transaction reap skipped (floor unreadable): %s", exc
        )
        return {"skipped": f"abandon_floor_unreadable:{type(exc).__name__}"}
    floor = max(live_next, binding_high, int(published_high or 0), ledger_floor)

    min_age_sec = float(days) * 24.0 * 3600.0
    now = time.time()
    reaped = 0
    freed = 0
    try:
        transaction_dirs = sorted(root.iterdir())
    except OSError:
        return {"reaped": 0, "bytes_freed": 0, "floor": floor}
    for transaction in transaction_dirs:
        name = transaction.name
        if not re.fullmatch(r"[0-9a-f]{64}", name):
            continue
        claim = _load_json(transaction / "claim.json")
        receipt = _load_json(transaction / "receipt.json")
        if not claim or not receipt:
            continue  # no-receipt / no-claim transactions are never reaped
        checkpoint = claim.get("checkpoint")
        try:
            next_v = (
                int(checkpoint.get("next_v") or 0)
                if isinstance(checkpoint, dict)
                else 0
            )
        except (TypeError, ValueError):
            next_v = 0
        if next_v <= 0 or next_v >= floor:
            continue
        if receipt.get("checkpoint_cleared") is not True:
            continue
        if receipt.get("candidate_state") != "quarantine":
            continue
        try:
            age = now - max(
                transaction.stat().st_mtime,
                (transaction / "receipt.json").stat().st_mtime,
            )
        except OSError:
            continue
        if age < min_age_sec:
            continue
        size = _path_size(transaction)
        try:
            _append_locked_jsonl_receipt(
                results_dir,
                REAPED_ABANDON_TXN_LEDGER_NAME,
                {
                    "schema_version": 1,
                    "kind": "abandon_transaction_reaped",
                    "reaped_at": now,
                    "transaction_id": name,
                    "next_v": next_v,
                    "candidate_state": "quarantine",
                    "checkpoint_cleared": True,
                    "claim_digest": claim.get("claim_digest"),
                    "receipt_digest": receipt.get("receipt_digest"),
                    "age_sec": round(age, 3),
                    "floor": floor,
                    "retention_days": float(days),
                    "bytes_freed": size,
                },
            )
        except Exception as exc:
            log.warning(
                "abandon transaction %s kept (receipt failed): %s", name, exc
            )
            continue
        gone = _rm(transaction)
        if gone:
            reaped += 1
            freed += gone
    return {"reaped": reaped, "bytes_freed": freed, "floor": floor}


def _events_auto_rotation_step(
    results_dir: Path, *, window: dict[str, Any]
) -> dict[str, Any] | None:
    """Digest-proven events auto-compression behind the A-track entry.

    Calls ``autonomous_rotate_append_only_sources(*, actor, sources=None,
    max_batch_bytes=None) -> list[dict]`` from
    ``evolution_infra_archive_rotation``. Gating:
    ``POK_EVENTS_AUTO_ROTATION_ENABLED`` (default 0 — the production step is
    inert until the operator flips it), the safe window open, and the
    critical disk tier. A missing entry (ImportError) degrades to
    skipped:"unavailable" plus a ``pipeline.events_auto_rotation_skipped``
    event, never an error. The entry re-heads live ``events.jsonl`` only
    after the dropped prefix is committed as a digest-bound gzip batch
    (``source_preserved_append_only`` is False).
    """
    if not _env_flag(EVENTS_ROTATION_ENV, "0"):
        return None
    if not window.get("open"):
        return {"skipped": "window_closed"}
    if not window.get("events_rotation_tier_allowed"):
        return {"skipped": "tier_not_critical"}
    try:
        from evolution_infra_archive_rotation import (
            autonomous_rotate_append_only_sources,
        )
    except ImportError as exc:
        _emit_governance_event(
            "pipeline.events_auto_rotation_skipped",
            "warn",
            f"events auto-rotation entry unavailable: {exc}",
            {"actor": EVENTS_ROTATION_ACTOR, "reason": "unavailable"},
        )
        return {"skipped": "unavailable"}
    try:
        receipts = autonomous_rotate_append_only_sources(
            actor=EVENTS_ROTATION_ACTOR
        )
    except Exception as exc:
        log.warning("events auto-rotation failed: %s", exc)
        return {"skipped": f"error:{type(exc).__name__}"}
    receipts = [row for row in (receipts or []) if isinstance(row, dict)]
    if receipts:
        _emit_governance_event(
            "pipeline.events_auto_rotation_done",
            "info",
            (
                "events auto-compression re-headed "
                f"{len(receipts)} append-only source(s)"
            ),
            {
                "actor": EVENTS_ROTATION_ACTOR,
                "sources": [
                    {
                        "source": Path(str(row.get("source") or "")).name,
                        "rotation_id": row.get("rotation_id"),
                        "start_offset": row.get("start_offset"),
                        "end_offset": row.get("end_offset"),
                        "plaintext_bytes": row.get("plaintext_bytes"),
                        "gzip_bytes": row.get("gzip_bytes"),
                        "source_preserved_append_only": row.get(
                            "source_preserved_append_only"
                        ),
                        "reheaded": row.get("reheaded"),
                    }
                    for row in receipts
                ],
            },
        )
    return {"rotated": len(receipts), "receipts": receipts}


def _append_reaped_tree_receipt(results_dir: Path, row: dict[str, Any]) -> None:
    """Durably append one orphan-tree receipt row under the sidecar lock."""
    _append_locked_jsonl_receipt(results_dir, REAPED_TREES_LEDGER_NAME, row)


def _reap_generation_trees(
    results_dir: Path,
    *,
    live: set[int],
    published: set[int],
    abandoned: list[int],
    keep_abandoned: int,
    orphan_max_age_sec: float = (
        DEFAULT_ORPHAN_RESULT_TREE_MAX_AGE_DAYS * 24.0 * 3600.0
    ),
) -> tuple[int, int]:
    freed = 0
    removed = 0
    keep = set(sorted(set(abandoned), reverse=True)[: max(0, keep_abandoned)])
    protected = set(live) | set(published) | keep
    try:
        children = list(results_dir.iterdir())
    except OSError:
        return freed, removed
    abandoned_set = set(abandoned)
    now = time.time()
    orphan_references: set[int] | None = None
    for child in children:
        try:
            if not child.is_dir() or child.is_symlink():
                continue
        except OSError:
            continue
        if not re.fullmatch(r"v\d+", child.name):
            continue
        version = _version_token(child.name)
        if version is None or version in protected:
            continue
        if version in abandoned_set:
            freed += _rm(child)
            removed += 1
            continue
        # T7a orphan branch: prepared but never published, absent from the
        # abandon ledger (a crashed generation leaves no receipt at all),
        # unreferenced by any live surface, and older than the age window.
        if orphan_max_age_sec <= 0:
            continue
        if orphan_references is None:
            orphan_references = _orphan_reference_versions(results_dir, live)
            if orphan_references is None:
                # Unreadable live-reference surface: never guess which tree
                # is orphaned. Reap nothing this pass.
                break
        if version in orphan_references:
            continue
        try:
            stat_result = child.stat()
            age = now - stat_result.st_mtime
        except OSError:
            continue
        if age < orphan_max_age_sec:
            continue
        if _tree_contains_strict_invocations(child):
            continue
        # Receipt first: a tree whose receipt cannot be durably written is
        # kept, never silently deleted.
        try:
            _append_reaped_tree_receipt(
                results_dir,
                {
                    "schema_version": 1,
                    "kind": "orphan_result_tree_reaped",
                    "reaped_at": now,
                    "version": version,
                    "tree": child.name,
                    "tree_mtime": stat_result.st_mtime,
                    "age_sec": round(age, 3),
                    "orphan_max_age_sec": float(orphan_max_age_sec),
                    "reason": "prepared_never_published_no_live_reference_aged_out",
                    "bytes_freed": _path_size(child),
                },
            )
        except Exception as exc:
            log.warning("orphan tree %s kept (receipt failed): %s", child.name, exc)
            continue
        freed += _rm(child)
        removed += 1
    return freed, removed


def _reap_crossover_workspaces(
    results_dir: Path,
    *,
    live: set[int],
    published: set[int],
    abandoned: set[int],
) -> tuple[int, int]:
    freed = 0
    removed = 0
    root = results_dir / "crossover_workspaces"
    if not root.is_dir() or root.is_symlink():
        return freed, removed
    try:
        children = list(root.iterdir())
    except OSError:
        return freed, removed
    for child in children:
        try:
            if not child.is_dir() or child.is_symlink():
                continue
        except OSError:
            continue
        version = _version_token(child.name)
        if version is None or version in live or version in published:
            continue
        if version in abandoned:
            freed += _rm(child)
            removed += 1
    return freed, removed


def _reap_tmp_and_stale_locks(results_dir: Path, *, max_age_sec: float = 3600.0) -> tuple[int, int]:
    freed = 0
    removed = 0
    now = time.time()
    try:
        children = list(results_dir.iterdir())
    except OSError:
        return freed, removed
    for child in children:
        try:
            if child.is_symlink() or not child.is_file():
                continue
            name = child.name
            if name in _PROTECTED_RESULT_NAMES:
                continue
            stale = (now - child.stat().st_mtime) > max_age_sec
        except OSError:
            continue
        if name.endswith(".tmp") or name.endswith(".hygiene.tmp"):
            # 2026-10-10 (w6 disk_hygiene_tmp_race): a FRESH .tmp is almost
            # certainly an in-flight atomic publish, not garbage. The
            # checkpoint writer's _atomic_publish_state_text
            # (evolution_infra_state_io.py) creates
            # ``.<name>.<uuid>.tmp`` with O_EXCL, writes+fsyncs, then
            # os.replace()s it — the whole sequence spans milliseconds. The
            # unconditional unlink here raced exactly that window on
            # 2026-10-10 05:59:32 (hygiene freed 21.4MB in the same minute
            # the writer's os.replace raised ENOENT and crashed the
            # orchestrator, losing a precommit_failed checkpoint write).
            # Gate .tmp reaping on the same max_age_sec staleness the lock
            # branch below already uses: an actively-written tmp (mtime now)
            # is skipped; a leftover from a crashed writer is >1h old and
            # still reaped.
            if not stale:
                continue
            freed += _rm(child)
            removed += 1
            continue
        if name.endswith(".json.lock") and stale:
            json_name = name[: -len(".lock")]
            if json_name in _PROTECTED_RESULT_NAMES:
                continue
            json_path = results_dir / json_name
            if not json_path.exists():
                freed += _rm(child)
                removed += 1
    return freed, removed


def _rotate_orchestrator_logs(logs_dir: Path | None, keep: int) -> tuple[int, int]:
    if logs_dir is None or not logs_dir.is_dir():
        return 0, 0
    try:
        # Import-order guard (2026-10-11): this janitor seam runs from the web
        # lifespan, potentially in a process where ``orchestrator`` has never
        # been imported (view-only / reset-required server). Importing the
        # companion FIRST starts the orchestrator<->companion import cycle the
        # wrong way round: orchestrator.py's bottom re-export block then raises
        # ImportError against the partially-initialized companion and leaves
        # permanently-stale companion ``_o`` bindings (a dead partial
        # orchestrator object). Importing ``orchestrator`` first initializes
        # the cluster in the safe order (identical weight: the companion
        # imports it at module level anyway).
        import orchestrator  # noqa: F401
        from orchestrator_abandon_and_cost import _rotate_orchestrator_logs as _rotate
    except Exception:
        _rotate = None
    before = {
        path.name: _path_size(path)
        for path in logs_dir.glob("orchestrator_*.txt")
        if path.is_file() and not path.is_symlink()
    }
    if _rotate is not None:
        try:
            _rotate(logs_dir, keep=keep)
        except Exception as exc:
            log.warning("orchestrator log rotate failed: %s", exc)
            return 0, 0
    after = {path.name for path in logs_dir.glob("orchestrator_*.txt")}
    freed = 0
    removed = 0
    for name, size in before.items():
        if name not in after:
            freed += size
            removed += 1
    return freed, removed


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_days_seconds(name: str, default_days: float) -> float:
    return max(0.0, _env_float(name, default_days)) * 24.0 * 3600.0


def _cost_receipt_digest(body: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in body.items() if key != "receipt_digest"}
    raw = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _write_cost_compaction_receipt(
    receipts_dir: Path, body: dict[str, Any]
) -> Path:
    receipts_dir.mkdir(parents=True, exist_ok=True)
    body["receipt_digest"] = _cost_receipt_digest(body)
    stamp = time.strftime(
        "%Y%m%dT%H%M%S", time.gmtime(body.get("created_at") or time.time())
    )
    path = receipts_dir / f"{stamp}-{uuid.uuid4().hex[:8]}.json"
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    descriptor = os.open(receipts_dir, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _new_cost_summary_bucket(month: str) -> dict[str, Any]:
    return {
        "row_count": 0,
        "cost_usd_sum": 0.0,
        "cost_known_rows": 0,
        "cost_unknown_rows": 0,
        "input_tokens_sum": 0,
        "output_tokens_sum": 0,
        "generation_ids": set(),
        "first_ts": None,
        "last_ts": None,
        "kind_counts": {},
    }


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _merge_cost_summary_into_summary(
    bucket: dict[str, Any], row: dict[str, Any]
) -> None:
    """Additively merge a summary row into a month bucket.

    On a fresh (all-zero) bucket this is an exact seed, so ledger line order
    — summary row before or after its month's full rows — cannot matter.
    """
    bucket["row_count"] += int(row.get("row_count") or 0)
    bucket["cost_usd_sum"] += _as_float(row.get("cost_usd_sum"))
    bucket["cost_known_rows"] += int(row.get("cost_known_rows") or 0)
    bucket["cost_unknown_rows"] += int(row.get("cost_unknown_rows") or 0)
    bucket["input_tokens_sum"] += int(row.get("input_tokens_sum") or 0)
    bucket["output_tokens_sum"] += int(row.get("output_tokens_sum") or 0)
    bucket["generation_ids"] |= {
        str(item) for item in (row.get("generation_ids") or []) if str(item)
    }
    for key, value in dict(row.get("kind_counts") or {}).items():
        clean = str(key)
        bucket["kind_counts"][clean] = (
            int(bucket["kind_counts"].get(clean, 0)) + int(value)
        )
    for edge in ("first_ts", "last_ts"):
        candidate = row.get(edge)
        try:
            candidate = float(candidate)
        except (TypeError, ValueError):
            continue
        if bucket[edge] is None:
            bucket[edge] = candidate
        elif edge == "first_ts":
            bucket[edge] = min(bucket[edge], candidate)
        else:
            bucket[edge] = max(bucket[edge], candidate)


def _merge_cost_row_into_summary(
    bucket: dict[str, Any], row: dict[str, Any]
) -> None:
    bucket["row_count"] += 1
    kind = str(row.get("kind") or "unknown")
    bucket["kind_counts"][kind] = int(bucket["kind_counts"].get(kind, 0)) + 1
    cost_known = bool(row.get("cost_known")) and row.get("cost_usd") is not None
    if cost_known:
        bucket["cost_known_rows"] += 1
        bucket["cost_usd_sum"] += _as_float(row.get("cost_usd"))
    else:
        bucket["cost_unknown_rows"] += 1
    for field, key in (("input_tokens", "input_tokens_sum"), ("output_tokens", "output_tokens_sum")):
        try:
            bucket[key] += int(row.get(field) or 0)
        except (TypeError, ValueError):
            continue
    generation_id = str(row.get("generation_id") or "").strip()
    if generation_id:
        bucket["generation_ids"].add(generation_id)
    try:
        ts = float(row.get("ts"))
    except (TypeError, ValueError):
        ts = None
    if ts is not None:
        bucket["first_ts"] = ts if bucket["first_ts"] is None else min(bucket["first_ts"], ts)
        bucket["last_ts"] = ts if bucket["last_ts"] is None else max(bucket["last_ts"], ts)


def _finalize_cost_summary(month: str, bucket: dict[str, Any], now: float) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": COST_LEDGER_SUMMARY_KIND,
        "month": month,
        "row_count": int(bucket["row_count"]),
        "cost_usd_sum": round(float(bucket["cost_usd_sum"]), 9),
        "cost_known_rows": int(bucket["cost_known_rows"]),
        "cost_unknown_rows": int(bucket["cost_unknown_rows"]),
        "input_tokens_sum": int(bucket["input_tokens_sum"]),
        "output_tokens_sum": int(bucket["output_tokens_sum"]),
        "generation_ids": sorted(bucket["generation_ids"]),
        "first_ts": bucket["first_ts"],
        "last_ts": bucket["last_ts"],
        "kind_counts": dict(sorted(bucket["kind_counts"].items())),
        "compacted_at": now,
    }


def compact_generation_cost_ledger(
    results_dir: Path,
    *,
    now: float | None = None,
    retention_sec: float | None = None,
    min_interval_sec: float | None = None,
) -> dict[str, Any]:
    """Compact aged rows of ``generation_cost_ledger.jsonl``.

    Full rows younger than the retention window stay byte-verbatim. Older
    rows become one per-month summary row per month, and a receipt under
    ``cost_ledger_compaction_receipts/`` records a per-row summary list
    (line digest plus accounting scalars) so no row silently disappears.
    The whole read-plan-receipt-rewrite runs under the exact
    ``_locked_state_sidecar`` exclusive sidecar lock every ledger reader and
    writer already takes, so it is serialized against
    ``record_generation_cost`` / ``_read_generation_entries``.
    """
    results_dir = Path(results_dir)
    report: dict[str, Any] = {
        "ok": True,
        "bytes_freed": 0,
        "removed": 0,
        "skipped": None,
    }
    if not _env_flag("POK_COST_LEDGER_COMPACTION_ENABLED", "1"):
        report["skipped"] = "disabled"
        return report
    path = results_dir / COST_LEDGER_NAME
    try:
        if not path.is_file() or path.is_symlink():
            report["skipped"] = "ledger_missing"
            return report
    except OSError:
        report["skipped"] = "ledger_stat_failed"
        return report
    now = time.time() if now is None else float(now)
    if retention_sec is None:
        retention_sec = _env_days_seconds(
            "POK_COST_LEDGER_RETENTION_DAYS", DEFAULT_COST_LEDGER_RETENTION_DAYS
        )
    if min_interval_sec is None:
        min_interval_sec = max(
            0.0,
            _env_float(
                "POK_COST_LEDGER_COMPACTION_MIN_INTERVAL_SEC",
                DEFAULT_COST_LEDGER_COMPACTION_MIN_INTERVAL_SEC,
            ),
        )
    receipts_dir = results_dir / COST_LEDGER_RECEIPTS_DIR_NAME
    if receipts_dir.is_dir() and min_interval_sec > 0.0:
        try:
            newest = max(
                item.stat().st_mtime
                for item in receipts_dir.iterdir()
                if item.is_file() and item.name.endswith(".json")
            )
        except (OSError, ValueError):
            newest = None
        if newest is not None and (time.time() - newest) < min_interval_sec:
            report["skipped"] = "interval"
            return report

    try:
        from evolution_infra import _locked_state_sidecar, locked_file
    except Exception as exc:
        report["ok"] = False
        report["skipped"] = f"import_failed:{type(exc).__name__}"
        log.warning("cost ledger compaction unavailable: %s", exc)
        return report

    cutoff = now - float(retention_sec)
    try:
        with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
            raw = path.read_bytes()
            if not raw:
                report["skipped"] = "ledger_empty"
                return report
            summaries: dict[str, dict[str, Any]] = {}
            months_touched: dict[str, int] = {}
            existing_summary_months: set[str] = set()
            replaced_summary_line_hashes: list[str] = []
            recent_lines: list[bytes] = []
            row_summaries: list[dict[str, Any]] = []
            for raw_line in raw.splitlines(keepends=True):
                try:
                    row = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    # Unparsable bytes are never dropped, never compacted.
                    recent_lines.append(raw_line)
                    continue
                if not isinstance(row, dict):
                    recent_lines.append(raw_line)
                    continue
                if row.get("kind") == COST_LEDGER_SUMMARY_KIND:
                    month = str(row.get("month") or "")
                    if re.fullmatch(r"\d{4}-\d{2}", month):
                        existing_summary_months.add(month)
                        # The summary line itself is derived data; its bytes
                        # are replaced by the merged re-emission. Record the
                        # replaced line digest so the receipt keeps a
                        # byte-level trail of every original ledger line.
                        replaced_summary_line_hashes.append(
                            hashlib.sha256(raw_line).hexdigest()
                        )
                        _merge_cost_summary_into_summary(
                            summaries.setdefault(
                                month, _new_cost_summary_bucket(month)
                            ),
                            row,
                        )
                        continue
                    # Malformed summary month: keep the line verbatim
                    # instead of dropping its accounting.
                    recent_lines.append(raw_line)
                    continue
                try:
                    ts = float(row.get("ts"))
                except (TypeError, ValueError):
                    ts = None
                if ts is None or ts >= cutoff:
                    recent_lines.append(raw_line)
                    continue
                month = time.strftime("%Y-%m", time.gmtime(ts))
                bucket = summaries.setdefault(month, _new_cost_summary_bucket(month))
                _merge_cost_row_into_summary(bucket, row)
                months_touched[month] = months_touched.get(month, 0) + 1
                row_summaries.append(
                    {
                        "line_sha256": hashlib.sha256(raw_line).hexdigest(),
                        "entry_id": row.get("entry_id"),
                        "generation_id": row.get("generation_id"),
                        "kind": row.get("kind"),
                        "role": row.get("role"),
                        "ts": row.get("ts"),
                        "cost_usd": row.get("cost_usd"),
                        "cost_known": bool(row.get("cost_known")),
                        "input_tokens": row.get("input_tokens"),
                        "output_tokens": row.get("output_tokens"),
                    }
                )
            if not row_summaries:
                report["skipped"] = "nothing_to_compact"
                return report
            new_lines: list[bytes] = []
            for month in sorted(summaries):
                summary = _finalize_cost_summary(
                    month, summaries[month], now
                )
                new_lines.append(
                    json.dumps(summary, sort_keys=True, ensure_ascii=False).encode(
                        "utf-8"
                    )
                    + b"\n"
                )
            new_lines.extend(recent_lines)
            new_bytes = b"".join(new_lines)
            receipt: dict[str, Any] = {
                "schema_version": 1,
                "kind": "cost_ledger_compaction",
                "created_at": time.time(),
                "evaluated_at": now,
                "retention_sec": float(retention_sec),
                "cutoff": cutoff,
                "ledger_path": str(path),
                "ledger_sha256_before": hashlib.sha256(raw).hexdigest(),
                "ledger_sha256_after": hashlib.sha256(new_bytes).hexdigest(),
                "bytes_before": len(raw),
                "bytes_after": len(new_bytes),
                "compacted_rows": len(row_summaries),
                "kept_recent_lines": len(recent_lines),
                "summary_rows_total": len(summaries),
                "merged_into_existing_summary_months": sorted(
                    months_touched.keys() & existing_summary_months
                ),
                "new_summary_months": sorted(
                    months_touched.keys() - existing_summary_months
                ),
                "replaced_summary_line_hashes": replaced_summary_line_hashes,
                "months": [
                    {"month": month, "rows_compacted": count}
                    for month, count in sorted(months_touched.items())
                ],
                "rows": row_summaries,
            }
            # Receipt first: a crash between the receipt and the ledger
            # rewrite can only duplicate receipt accounting, never lose a
            # row; the rewrite is idempotent on the next pass.
            receipt_path = _write_cost_compaction_receipt(receipts_dir, receipt)
            with locked_file(path, "wb") as handle:
                handle.write(new_bytes)
    except OSError as exc:
        report["ok"] = False
        report["skipped"] = f"io_error:{type(exc).__name__}:{exc}"
        log.warning("cost ledger compaction failed: %s", exc)
        return report
    report["bytes_freed"] = max(0, len(raw) - len(new_bytes))
    report["removed"] = len(row_summaries)
    report["compacted_rows"] = len(row_summaries)
    report["kept_recent_lines"] = len(recent_lines)
    report["bytes_before"] = len(raw)
    report["bytes_after"] = len(new_bytes)
    report["receipt_path"] = str(receipt_path)
    return report


def run_disk_hygiene(
    results_dir: Path,
    *,
    logs_dir: Path | None = None,
    bots_dir: Path | None = None,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    keep_saturator_sessions: int | None = None,
    keep_abandoned_result_dirs: int | None = None,
    keep_findings_lines: int = DEFAULT_KEEP_FINDINGS_LINES,
    keep_metrics_lines: int = DEFAULT_KEEP_METRICS_LINES,
    keep_orchestrator_logs: int = DEFAULT_KEEP_ORCHESTRATOR_LOGS,
    orphan_result_tree_max_age_sec: float | None = None,
) -> dict[str, Any]:
    """Reap non-authority runtime artifacts. Safe to call concurrently with LLM."""
    results_dir = Path(results_dir)
    report: dict[str, Any] = {
        "ok": True,
        "pressure": False,
        "bytes_freed": 0,
        "removed": 0,
        "free_bytes": None,
        "skipped": None,
    }
    if not results_dir.is_dir():
        report["ok"] = False
        report["skipped"] = "results_dir_missing"
        return report

    free = disk_free_bytes(results_dir)
    report["free_bytes"] = free
    pressure = free is not None and free < int(min_free_bytes)
    report["pressure"] = pressure

    if keep_saturator_sessions is None:
        keep_saturator_sessions = (
            DEFAULT_KEEP_SATURATOR_SESSIONS_PRESSURE
            if pressure
            else DEFAULT_KEEP_SATURATOR_SESSIONS
        )
    if keep_abandoned_result_dirs is None:
        keep_abandoned_result_dirs = (
            DEFAULT_KEEP_ABANDONED_RESULT_DIRS_PRESSURE
            if pressure
            else DEFAULT_KEEP_ABANDONED_RESULT_DIRS
        )

    live = live_protected_versions(results_dir)
    published = published_bot_versions(bots_dir)
    abandoned = abandoned_versions(results_dir)
    report["live_versions"] = sorted(live)
    report["published_high_water"] = max(published) if published else 0
    if orphan_result_tree_max_age_sec is None:
        orphan_result_tree_max_age_sec = _env_days_seconds(
            "POK_ORPHAN_RESULT_TREE_MAX_AGE_DAYS",
            DEFAULT_ORPHAN_RESULT_TREE_MAX_AGE_DAYS,
        )

    steps = (
        _prune_saturator(
            results_dir,
            keep_sessions=keep_saturator_sessions,
            keep_findings_lines=keep_findings_lines,
        ),
        _reap_orphan_drafts(results_dir, published=published),
        _reap_stale_consumer_checkpoints(
            results_dir, live=live, published=published
        ),
        _reap_generation_trees(
            results_dir,
            live=live,
            published=published,
            abandoned=abandoned,
            keep_abandoned=keep_abandoned_result_dirs,
            orphan_max_age_sec=orphan_result_tree_max_age_sec,
        ),
        _reap_crossover_workspaces(
            results_dir,
            live=live,
            published=published,
            abandoned=set(abandoned),
        ),
        _reap_tmp_and_stale_locks(results_dir),
        _rotate_orchestrator_logs(logs_dir, keep_orchestrator_logs),
    )
    for freed, removed in steps:
        report["bytes_freed"] += int(freed)
        report["removed"] += int(removed)

    compaction = _compact_candidate_ledger_step(results_dir)
    if compaction and compaction.get("receipt_persisted"):
        report["candidate_ledger_compaction"] = compaction

    # Governance safe window (2026-10-21): one predicate for the destructive
    # governance steps. (2) identity retention runs at every tier (it only
    # *stages* into the retired area; the critical tier merely caps its keep
    # window); the retired purge, abandon-transaction reap and events
    # auto-rotation require the open window plus their tier.
    window = janitor_safe_window(
        results_dir, published=published, free_bytes=free
    )
    report["janitor_window"] = {
        "open": window["open"],
        "tier": window["tier"],
        "closed_reasons": window["closed_reasons"],
    }

    retention = _evaluation_identity_retention_step(results_dir, window=window)
    if retention and retention.get("moved"):
        report["evaluation_identity_retention"] = {
            "moved": len(retention["moved"]),
            "receipt_path": retention.get("receipt_path"),
        }

    try:
        retired_purge = _purge_retired_identity_snapshots(
            results_dir, window=window
        )
        if retired_purge is not None:
            report["evaluation_identity_retired_purge"] = retired_purge
            if retired_purge.get("bytes_freed"):
                report["bytes_freed"] += int(retired_purge["bytes_freed"])
                report["removed"] += int(len(retired_purge.get("purged") or []))
    except Exception as exc:
        log.warning("retired identity purge failed: %s", exc)

    try:
        txn_reap = _reap_aged_abandon_transactions(
            results_dir,
            window=window,
            published_high=max(published) if published else 0,
        )
        if txn_reap is not None:
            report["abandon_transaction_reap"] = txn_reap
            if txn_reap.get("bytes_freed"):
                report["bytes_freed"] += int(txn_reap["bytes_freed"])
                report["removed"] += int(txn_reap.get("reaped") or 0)
    except Exception as exc:
        log.warning("abandon transaction reap failed: %s", exc)

    try:
        rotation_report = _events_auto_rotation_step(
            results_dir, window=window
        )
        if rotation_report is not None:
            report["events_auto_rotation"] = rotation_report
    except Exception as exc:
        log.warning("events auto-rotation step failed: %s", exc)

    metrics = results_dir / "llm_call_metrics.jsonl"
    trimmed = _trim_jsonl(metrics, keep_metrics_lines)
    if trimmed:
        report["bytes_freed"] += trimmed
        report["removed"] += 1

    # T7b: bounded retention for the generation cost ledger (recent rows
    # verbatim, aged rows compacted to per-month summaries + per-row
    # receipts, all under the ledger sidecar lock). Failures never break
    # the janitor loop.
    try:
        cost_report = compact_generation_cost_ledger(results_dir)
        report["cost_ledger_compaction"] = cost_report
        if cost_report.get("ok"):
            report["bytes_freed"] += int(cost_report.get("bytes_freed") or 0)
            report["removed"] += int(cost_report.get("removed") or 0)
    except Exception as exc:
        log.warning("cost ledger compaction failed: %s", exc)

    # Conservative retention window for the workflow journal store
    # (events.sqlite3 + content-addressed artifacts).  The step plans and
    # deletes only terminal, retention-aged, unreferenced runs and writes its
    # own receipt; failures never break the janitor loop.
    if _env_flag("POK_WORKFLOW_JOURNAL_HYGIENE_ENABLED", "1"):
        try:
            from workflow_journal_hygiene import (
                run_workflow_journal_hygiene,
            )

            journal_report = run_workflow_journal_hygiene(results_dir)
            report["workflow_journal"] = journal_report
            if journal_report.get("ok"):
                report["bytes_freed"] += int(
                    journal_report.get("bytes_freed") or 0
                )
                report["removed"] += int(journal_report.get("removed") or 0)
        except Exception as exc:
            log.warning("workflow journal hygiene failed: %s", exc)

    return report


async def run_disk_hygiene_loop(shutdown_mgr=None) -> None:
    """Process-lifetime loop. Never raises into the lifespan."""
    if not _env_flag("POK_DISK_HYGIENE_ENABLED", "1"):
        log.info("disk hygiene disabled (POK_DISK_HYGIENE_ENABLED)")
        return
    try:
        interval = float(os.environ.get("POK_DISK_HYGIENE_INTERVAL_SEC", str(DEFAULT_INTERVAL_SEC)))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_SEC
    interval = max(30.0, interval)
    try:
        min_free_gb = float(os.environ.get("POK_DISK_MIN_FREE_GB", "4"))
    except (TypeError, ValueError):
        min_free_gb = 4.0
    min_free_bytes = int(max(0.5, min_free_gb) * 1024 * 1024 * 1024)

    log.info(
        "disk hygiene started (interval=%.0fs min_free_gb=%.1f)",
        interval,
        min_free_gb,
    )

    def _once() -> dict[str, Any]:
        from evolution_infra import BOTS_DIR, RESULTS_DIR

        logs_dir = Path(__file__).resolve().parent.parent / "logs"
        return run_disk_hygiene(
            RESULTS_DIR,
            logs_dir=logs_dir,
            bots_dir=BOTS_DIR,
            min_free_bytes=min_free_bytes,
        )

    while not (
        shutdown_mgr is not None
        and getattr(shutdown_mgr, "is_shutting_down", False)
    ):
        try:
            report = await asyncio.to_thread(_once)
            freed = int(report.get("bytes_freed") or 0)
            if freed or report.get("pressure"):
                log.info(
                    "disk hygiene: freed=%d removed=%d pressure=%s free=%s",
                    freed,
                    int(report.get("removed") or 0),
                    bool(report.get("pressure")),
                    report.get("free_bytes"),
                )
                try:
                    from system_log import log_system_event

                    log_system_event(
                        "pipeline.disk_hygiene_done",
                        "info" if not report.get("pressure") else "warn",
                        (
                            "Disk hygiene reaped non-authority artifacts "
                            f"(freed={freed} pressure={report.get('pressure')})"
                        ),
                        {
                            "bytes_freed": freed,
                            "removed": report.get("removed"),
                            "pressure": report.get("pressure"),
                            "free_bytes": report.get("free_bytes"),
                        },
                    )
                except Exception:
                    pass
        except Exception as exc:
            log.warning("disk hygiene cycle failed: %s", exc)
        slept = 0.0
        while slept < interval:
            if shutdown_mgr is not None and getattr(shutdown_mgr, "is_shutting_down", False):
                break
            await asyncio.sleep(1.0)
            slept += 1.0
