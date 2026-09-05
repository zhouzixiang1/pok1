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

It runs from the app lifespan beside the LLM saturator so it keeps working
even when the orchestrator is mid-call. Failures are logged and swallowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
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
    "policy_epoch_reset_receipt.json",
    "policy_epoch_reconciliation_receipt.json",
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


def _reap_generation_trees(
    results_dir: Path,
    *,
    live: set[int],
    published: set[int],
    abandoned: list[int],
    keep_abandoned: int,
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
        if version not in abandoned_set:
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

    metrics = results_dir / "llm_call_metrics.jsonl"
    trimmed = _trim_jsonl(metrics, keep_metrics_lines)
    if trimmed:
        report["bytes_freed"] += trimmed
        report["removed"] += 1

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
