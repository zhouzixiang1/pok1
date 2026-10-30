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
# Two real in-log forms of the SELECTED plan (never the ensemble tail block):
# the durable sealed binding inside the audit prompt, and the accepted
# final_plan object in the master log.
_BINDING_HEAD_RE = re.compile(r'"(?:proposal_binding|final_plan)"\s*:\s*\{')
_ENSEMBLE_KEY_RE = re.compile(r'"proposal_ensemble"')


def _sealed_symbol_from_log(path: "Path | None") -> "str | None":
    """Extract the SEALED selected-plan symbol from a master/audit log.

    Scans each ``proposal_binding`` / ``final_plan`` object window for the
    change_symbol; windows that contain a ``proposal_ensemble`` render (all
    three proposals' contracts) are skipped, because a regex search there
    lands on proposal #3 — the extraction that was 12/12 wrong in production
    (2026-10-28 audit; the bare last-match tail scan is the disproven form).
    Returns None when no unambiguous selected-plan object exists — the caller
    must SKIP the version, never guess.
    """
    if path is None or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for m in _BINDING_HEAD_RE.finditer(text):
        window = text[m.end():m.end() + 6000]
        if _ENSEMBLE_KEY_RE.search(window):
            continue
        sm = _CHANGE_SYMBOL_RE.search(window)
        if sm:
            return sm.group(1)
    return None
_LEDGER_NAME = "direction_ledger.jsonl"


def _ledger_path() -> "Path | None":
    try:
        from evolution_infra import RESULTS_DIR

        return Path(RESULTS_DIR) / _LEDGER_NAME
    except Exception:
        return None


def _results_base(results_dir: "str | Path | None" = None) -> "Path | None":
    """Resolve the RESULTS_DIR that holds the ledger, abandon ledger and reports.

    The historical caller convention passes either the results DIRECTORY or an
    explicit ledger FILE path (``_ledger_path()`` returns a file, and
    ``record_planned_direction``/``_load_ledger_rows`` accept both), so every
    sibling-store reader must normalize first.  Without the ``.name`` guard a
    default (no-``results_dir``) call resolves ``direction_ledger.jsonl /
    abandoned_versions.jsonl`` — a path that never exists — and the reader
    silently returns empty.  Production call sites all omit ``results_dir``
    (agent_master.py, agent_master_ensemble.py, audit_agents.py,
    strict_authority_workflow.py), so the guard is load-bearing for the
    outcome-weighted veto, not cosmetic (2026-10-30 deploy verification).
    """

    if results_dir is not None:
        return Path(results_dir)
    try:
        from evolution_infra import RESULTS_DIR

        return Path(RESULTS_DIR)
    except Exception:
        lp = _ledger_path()
        return lp.parent if lp is not None else None


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
        # 2026-10-29 second-round audit guard: the fallback must never use the
        # disproven tail scan (last "change_symbol" match = ensemble proposal
        # #3, NOT the selected plan — 12/12 wrong). Sealed extraction from a
        # proposal_binding object only; a tree without a findable binding is
        # skipped, never mislabeled.
        sealed_symbol = None
        for candidate in (
            d / "logs" / "master_plan_audit_io.txt",
            log,
        ):
            sealed_symbol = _sealed_symbol_from_log(candidate)
            if sealed_symbol is not None:
                break
        if sealed_symbol is None:
            continue
        rows.append((v, sealed_symbol))
    return rows


def recent_symbol_counts(
    max_versions: int = 6,
    *,
    results_dir: "str | Path | None" = None,
    exclude_version: int | None = None,
) -> "dict[str, int]":
    """Recycling counts keyed by change symbol over the recent window.

    Mode-dispatched (queue #4, 2026-10-30) via ``POK_DIRECTION_VETO_MODE``:

    - ``attempt`` (code default, byte-identical history): every planned slot
      counts, whatever happened to it — how RECENTLY a direction was tried.
    - ``outcome``: the queue #4 outcome-weighted W12 view — only native-gate
      deaths increment, a publication outranks older attempts, plan-stage
      deaths count 0.  The window widens to ``POK_DIRECTION_VETO_WINDOW``
      (default 12) regardless of ``max_versions``; the threshold (>=2) and
      the consumers' code strings are unchanged, so the ensemble, the audit,
      the hard-avoid render, and the strict-authority replay (which must
      never byte-drift mid-generation) all flip together through this one
      function — there is no live-vs-replay semantic split to reconcile.
    """

    if veto_mode() == "outcome":
        return recent_symbol_failure_counts(
            veto_window(),
            results_dir=results_dir,
            exclude_version=exclude_version,
        )
    counts: "dict[str, int]" = {}
    for _v, symbol in recent_change_symbols(
        max_versions, results_dir=results_dir, exclude_version=exclude_version
    ):
        counts[symbol] = counts.get(symbol, 0) + 1
    return counts


VETO_MODE_ENV = "POK_DIRECTION_VETO_MODE"
VETO_WINDOW_ENV = "POK_DIRECTION_VETO_WINDOW"
VETO_MODES = ("attempt", "outcome")


def veto_mode() -> str:
    """``attempt`` (W6 plan-attempt counting) or ``outcome`` (W12 weighted).

    Unknown values fail closed to ``attempt`` — the historical, more
    permissive-on-novel-but-refuted directions semantics is NOT the safe
    side; the safe side is the byte-identical default: a typo in the env key
    must not silently enable an untested veto.
    """

    raw = str(os.environ.get(VETO_MODE_ENV) or "").strip().lower()
    return raw if raw in VETO_MODES else "attempt"


def veto_window() -> int:
    """Generation window used in ``outcome`` mode (12 default, 6..24 bound)."""

    raw = str(os.environ.get(VETO_WINDOW_ENV) or "").strip()
    try:
        n = int(raw)
    except ValueError:
        return 12
    return n if 6 <= n <= 24 else 12


def veto_rule_text(seen: int, window: "int | None" = None) -> str:
    """One canonical prose rendering of the fired veto, mode-aware.

    All four consumer prompts/messages render through here so a scout, an
    audit contradiction, and a repair hint can never describe different
    rules than the code applied.
    """

    mode = veto_mode()
    if mode == "outcome":
        win = int(window if window is not None else veto_window())
        return (
            f"already refuted by the native gates in {seen} of the last {win} "
            "generation attempts (outcome-weighted directions ledger: only "
            "precommit/quality-breaker deaths count, a published success "
            "outranks older failures, plan-stage deaths are noise)"
        )
    win = int(window if window is not None else 6)
    return f"already targeted in >= 2 of the last {win} generation attempts"


# Reasons that count as a NATIVE-GATE failure for the outcome-weighted view.
# Both breakers are reached only after the candidate cleared planning, so the
# direction itself was graded and then REFUTED by evaluation evidence:
# ``precommit_rework_circuit_breaker`` (the 2-round precommit cap) and
# ``quality_rework_circuit_breaker``. Everything else — ``master_audit_rejected``
# (the audit's own repetitive veto among them), plan-stage exhaustion,
# infrastructure abandonment — is a PROCESS failure that says nothing about
# whether the symbol works at the tables, and never increments.
_NATIVE_GATE_FAILURE_REASONS = frozenset(
    {
        "precommit_rework_circuit_breaker",
        "quality_rework_circuit_breaker",
    }
)


def _abandon_outcomes(results_dir: "str | Path | None") -> "dict[int, str]":
    """version -> last abandon reason class (a mid-flight version is absent).

    Best effort: an unreadable ledger yields an empty map, which the caller
    reads as "no deaths" — the conservative direction for a veto (a symbol
    with unknown outcome is never condemned, matching the fail-open posture
    of ``recent_symbol_counts``).
    """

    base = _results_base(results_dir)
    if base is not None and base.name == _LEDGER_NAME:
        base = base.parent
    if base is None:
        return {}
    path = base / "abandoned_versions.jsonl"
    outcomes: "dict[int, str]" = {}
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
                if type(version) is not int:
                    continue
                reason = str(row.get("reason") or "")
                # Reason strings carry a free-text tail on some producers
                # (``master_audit_rejected v642: ...``); the class is the
                # first token.
                outcomes[version] = reason.split(":", 1)[0].split(" ", 1)[0].strip()
    except OSError:
        return {}
    return outcomes


def published_version_set(
    results_dir: "str | Path | None" = None,
) -> "set[int]":
    """Versions with an evaluation report showing an empty blocker list.

    ``passed`` is deliberately NOT trusted as the signal — legacy report
    writers left it unset on a publishing pass (v629.json: no ``passed``
    key, ``blockers: []`` while the version carries a completion tag).
    The report files are one-per-version (``v<N>.json`` and the F1
    ``v<N>_round<k>.json`` resamples); an attempted-but-abandoned version
    keeps its last failing report, so a round file with blockers never
    marks a version published.
    """

    base = _results_base(results_dir)
    if base is not None and base.name == _LEDGER_NAME:
        base = base.parent
    if base is None:
        return set()
    rep_dir = base / "precommit_reports"
    published: "set[int]" = set()
    try:
        names = os.listdir(rep_dir)
    except OSError:
        return set()
    for name in names:
        m = re.fullmatch(r"v(\d+)(?:_round\d+)?\.json", name)
        if not m:
            continue
        try:
            row = json.loads((rep_dir / name).read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        blockers = row.get("blockers")
        if isinstance(blockers, list) and not blockers:
            published.add(int(m.group(1)))
    return published


def recent_symbol_failure_counts(
    max_versions: int = 12,
    *,
    results_dir: "str | Path | None" = None,
    exclude_version: int | None = None,
    override: "dict[str, int] | None" = None,
) -> "dict[str, int]":
    """Outcome-weighted per-symbol native-gate failure counts (queue #4).

    The W6 attempt-counting veto (``recent_symbol_counts``) grades a symbol
    by how RECENTLY it was planned, regardless of how the attempt ended.
    Real replay (2026-10-30, ledger + abandon ledger + precommit reports,
    v628-v643 window): ``_candidate_raise_fractions`` was sealed for v641
    while its W6 count read 0 — v630/v633/v634 deaths had already slid out
    of the six-slot window — and v641 then burned a full generation dying on
    the same symbol. Meanwhile v636's clean ``_realized_call_equity``
    publication (v625/v628 earlier failures) shows a published success
    genuinely rehabilitates a symbol.

    Semantics (calibrated on that replay):
    - only NATIVE-GATE deaths increment (``_NATIVE_GATE_FAILURE_REASONS``):
      the candidate reached evaluation and was refuted there;
    - a symbol's most recent outcome DOMINATES: walking newest-first, the
      first publication seen for a symbol freezes it at its count-so-far
      (a publication outranks everything older — v636-style rehabilitation
      survives);
    - plan-stage deaths (audit rejects, exhaustion, infra) count 0 — they
      carry no table evidence;
    - the window is 12 generation slots (newest-first, ``exclude_version``
      still skips the generation currently being planned);
    - a version still mid-flight at the newest edge counts 0 (unknown
      outcome must not veto).

    ``override`` lets tests/callers supply the map directly; when supplied
    it is returned as-is (positive counts only). Never raises: unreadable
    stores yield ``{}`` and the callers stay fail-open.
    """

    if override is not None:
        return {k: int(v) for k, v in override.items() if int(v) > 0}
    try:
        rows = recent_change_symbols(
            max_versions, results_dir=results_dir, exclude_version=exclude_version
        )
        deaths = _abandon_outcomes(results_dir)
        published = published_version_set(results_dir)
    except Exception:
        return {}
    counts: "dict[str, int]" = {}
    decided: "set[str]" = set()
    for version, symbol in rows:  # newest-first
        if symbol in decided:
            continue  # this symbol's most recent outcome was a publication;
            # everything older than it is outranked and must not re-increment.
        if version in published:
            decided.add(symbol)
            continue
        if deaths.get(version) in _NATIVE_GATE_FAILURE_REASONS:
            counts[symbol] = counts.get(symbol, 0) + 1
        # else: plan-stage death or alive/unaccounted — counts 0, no increment.
    return {symbol: n for symbol, n in counts.items() if n > 0}
