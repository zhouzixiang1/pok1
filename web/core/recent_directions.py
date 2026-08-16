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

import re
from pathlib import Path

_CHANGE_SYMBOL_RE = re.compile(
    r'"change_symbol"\s*:\s*"(policy\.py:[A-Za-z_][A-Za-z0-9_]*)"'
)
_VERSION_DIR_RE = re.compile(r"v\d+")


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
) -> "list[tuple[int, str]]":
    """[(version, change_symbol), ...] newest-first for recent generations.

    The last ``"change_symbol"`` occurrence in each master log tail is the
    final selected plan. Best-effort: unreadable or absent logs are skipped;
    never raises.
    """
    if results_dir is None:
        try:
            from evolution_infra import RESULTS_DIR

            results_dir = RESULTS_DIR
        except Exception:
            return []
    try:
        results = Path(results_dir)
        version_dirs = sorted(
            (
                d for d in results.iterdir()
                if d.is_dir() and _VERSION_DIR_RE.fullmatch(d.name)
            ),
            key=lambda d: int(d.name[1:]),
            reverse=True,
        )[:max_versions]
    except OSError:
        return []
    rows: "list[tuple[int, str]]" = []
    for d in version_dirs:
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
            rows.append((int(d.name[1:]), symbols[-1]))
    return rows


def recent_symbol_counts(
    max_versions: int = 6,
    *,
    results_dir: "str | Path | None" = None,
) -> "dict[str, int]":
    """Recycling counts keyed by change symbol over the last N generations."""
    counts: "dict[str, int]" = {}
    for _v, symbol in recent_change_symbols(
        max_versions, results_dir=results_dir
    ):
        counts[symbol] = counts.get(symbol, 0) + 1
    return counts
