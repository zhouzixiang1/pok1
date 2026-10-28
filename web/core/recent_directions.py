"""Shared cross-generation direction ledger (system-owned, advisory).

Extracts the change symbols recent generations actually targeted — published
AND abandoned alike (abandoned work leaves no completion tag, so tag history
alone is blind to it) — from the strict master logs under
``RESULTS_DIR/v<N>/logs/master_io.txt``.

2026-08-16 evolution audit: v170-v187 proposals recycled 63% into
``opponent.terminal_response``; ``_bluff_allowed`` was re-proposed in 7
generations and ``_decision_from_equity`` in 11 rounds; scout triples were
near-identical in v172/v186/v187 — because nothing told planning what had
already been tried. Consumers:

- ``generation_scheduler._recent_directions_block`` renders the advisory
  master-context block;
- ``agent_master_ensemble`` dedupes within-ensemble duplicate targets and
  pins repair retries to the original symbol family;
- ``audit_agents._run_master_plan_audit`` scores direction novelty against
  this ledger (direction-scoped, not parent-scoped).

Deliberately importable from any of those modules: heavy imports
(``evolution_infra``) happen lazily inside functions so this module never
completes an import cycle.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

_CHANGE_SYMBOL_RE = re.compile(
    r'"change_symbol"\s*:\s*"(policy\.py:[A-Za-z_][A-Za-z0-9_]*)"'
)
_VERSION_DIR_RE = re.compile(r"v\d+")
_LEDGER_NAME = "direction_ledger.jsonl"


def _ledger_path() -> "Path | None":
    try:
        from evolution_infra import RESULTS_DIR

        return Path(RESULTS_DIR) / _LEDGER_NAME
    except Exception:
        return None


def record_planned_direction(
    version: int,
    source_v: "int | None",
    proposal_binding: "dict | None",
    *,
    results_dir: "str | Path | None" = None,
) -> bool:
    """Durably record the SEALED selected symbol at plan acceptance.

    The tail-scan extraction in ``recent_change_symbols`` proved 12/12 wrong
    (2026-10-28 audit): the tail of ``master_io.txt`` is the per-proposal
    compilation-contract block, so the ledger recorded ensemble proposal #3,
    never the selected plan. The sealed ``proposal_binding.change_symbol`` is
    the only trustworthy value, and it exists exactly at plan acceptance —
    here. A durable line also survives the generation-tree reaping that
    silently blanked the ledger window (24 missing v-trees, v599-v624).

    One row per version; a re-allocated version rewrites its old row (the
    prior attempt is separately owned by the abandon ledger). Returns True
    when a usable row was written.
    """

    symbol = ""
    selected_id = ""
    if isinstance(proposal_binding, dict):
        raw_symbol = str(proposal_binding.get("change_symbol") or "")
        if re.fullmatch(r"policy\.py:[A-Za-z_][A-Za-z0-9_]*", raw_symbol):
            symbol = raw_symbol
        selected_id = str(proposal_binding.get("selected_proposal_id") or "")
    if not symbol:
        return False
    base = Path(results_dir) if results_dir is not None else _ledger_path()
    if base is None:
        return False
    path = base if base.name == _LEDGER_NAME else base / _LEDGER_NAME
    version = int(version)
    row = {
        "version": version,
        "source_v": int(source_v) if source_v is not None else None,
        "change_symbol": symbol,
        "selected_proposal_id": selected_id,
        "ts": time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        kept: "list[str]" = []
        if path.is_file():
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        existing = json.loads(stripped)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(existing, dict):
                        continue
                    if existing.get("version") == version:
                        continue
                    kept.append(stripped)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for line in kept:
                fh.write(line + "\n")
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def _load_ledger_rows(results_dir: "str | Path | None") -> "dict[int, str]":
    """Sealed-symbol rows keyed by version (last row wins per version)."""
    base = Path(results_dir) if results_dir is not None else _ledger_path()
    if base is None:
        return {}
    path = base if base.name == _LEDGER_NAME else base / _LEDGER_NAME
    rows: "dict[int, str]" = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                version = row.get("version")
                symbol = row.get("change_symbol")
                if (
                    type(version) is int
                    and isinstance(symbol, str)
                    and re.fullmatch(r"policy\.py:[A-Za-z_][A-Za-z0-9_]*", symbol)
                ):
                    rows[version] = symbol
    except OSError:
        return {}
    return rows


def published_versions(repo_root: "str | Path | None" = None) -> "set[int]":
    """Versions carrying an annotated completion tag (cached nowhere; cheap)."""
    import subprocess

    if repo_root is None:
        try:
            from evolution_infra import PROJECT_ROOT

            repo_root = PROJECT_ROOT
        except Exception:
            return set()
    versions: "set[int]" = set()
    try:
        out = subprocess.run(
            ["git", "tag", "--list", "national-cloud-bot-v*"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            try:
                versions.add(int(line.rsplit("-v", 1)[1]))
            except (ValueError, IndexError):
                continue
    except Exception:
        pass
    return versions


def recent_change_symbols(
    max_versions: int = 12,
    *,
    results_dir: "str | Path | None" = None,
    exclude_version: "int | None" = None,
) -> "list[tuple[int, str]]":
    """[(version, change_symbol), ...] newest-first for recent generations.

    Authority order (2026-10-28 fix): durable ``direction_ledger.jsonl``
    rows — the SEALED ``proposal_binding.change_symbol`` captured at plan
    acceptance — beat the log tail-scan. The tail scan proved 12/12 wrong on
    real generations: the tail of ``master_io.txt`` is the per-proposal
    compilation-contract block, so ``symbols[-1]`` recorded ensemble proposal
    #3, never the selected plan, and every consumer (audit repetitive veto,
    master-context block) ran on mislabeled history. It remains only as the
    pre-ledger fallback for versions that predate the durable rows. Best
    effort; never raises.
    """
    if results_dir is None:
        try:
            from evolution_infra import RESULTS_DIR

            results_dir = RESULTS_DIR
        except Exception:
            return []
    sealed = _load_ledger_rows(results_dir)
    rows: "list[tuple[int, str]]" = []
    for version, symbol in sorted(sealed.items(), key=lambda kv: -kv[0]):
        # exclude_version: the generation currently being planned/audited
        # must not count itself — the audit's repetitive-veto otherwise
        # blocks any symbol after ONE prior attempt (static audit finding 6).
        if exclude_version is not None and version == int(exclude_version):
            continue
        rows.append((version, symbol))
        if len(rows) >= max_versions:
            return rows
    try:
        results = Path(results_dir)
        version_dirs = sorted(
            (
                d for d in results.iterdir()
                if d.is_dir() and _VERSION_DIR_RE.fullmatch(d.name)
                and int(d.name[1:]) not in sealed
            ),
            key=lambda d: int(d.name[1:]),
            reverse=True,
        )
    except OSError:
        return rows
    for d in version_dirs:
        if len(rows) >= max_versions:
            break
        v = int(d.name[1:])
        if exclude_version is not None and v == int(exclude_version):
            continue
        log = d / "logs" / "master_io.txt"
        if not log.is_file():
            continue
        try:
            size = log.stat().st_size
            with log.open("rb") as fh:
                fh.seek(max(0, size - 400_000))
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        symbols = _CHANGE_SYMBOL_RE.findall(tail)
        if symbols:
            rows.append((v, symbols[-1]))
    return rows


def recent_symbol_counts(
    max_versions: int = 6,
    *,
    results_dir: "str | Path | None" = None,
    exclude_version: int | None = None,
) -> "dict[str, int]":
    """Recycling counts keyed by change symbol over the last N generations."""
    counts: "dict[str, int]" = {}
    for _v, symbol in recent_change_symbols(
        max_versions, results_dir=results_dir, exclude_version=exclude_version
    ):
        counts[symbol] = counts.get(symbol, 0) + 1
    return counts
