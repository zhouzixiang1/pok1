#!/usr/bin/env python3
"""Dual-gate replay for the audit_scope fix (2026-10-04).

Replays the two real failure classes against REAL runtime products and
proves the fix on both gates:

  PRE  (pre-audit_scope behavior, reconstructed over the legacy citable-text
        view that flattened the sealed ``proposal_ensemble`` /
        ``proposal_binding`` into the audit text):
        A. a correct plan — every number system-derived and snapshot-exact —
           is FALSELY rejected because the pair-alias window swallows the
           next sealed binding's aggregate ``games`` leaf.  This is the real
           v489 rejection class (``national_cloud_v11 vs national_cloud_v27
           cited games=404, snapshot has games=34``; generation abandoned
           ``master_audit_rejected``), replayed on the real plan bytes and
           the real frozen snapshot of the newest generation.
        A2. the real v489 wrong-prose numbers planted on the plan's own
            text are repaired by the real normalization seam, yet the legacy
            audit view STILL rejects the plan from the sealed window —
            exactly how v489 died after its citations were corrected.
        B. the v488 "re_seal" temptation — rewriting the sealed stale number
            to satisfy the audit — fails BOTH the quality-gate identity
            re-derivation (``_proposal_identity`` / scout
            ``role_result_digest``) and the sealed-evidence precision check.
            (Provenance: the real 2026 v488 ``pipeline.master_citations_normalized``
            event rewrote four sealed ``snapshot_evidence`` leaves and killed
            the quality gate with ``proposal_identity_mismatch`` /
            ``proposal_invocation_result_mismatch``.)

  POST (fixed tree, live functions from web/core):
        the same plan passes the prose-citation accuracy audit, the
        two-tier statistical floor (aggregate leg satisfied by the
        structured sealed citation reference), the byte-level sealed-evidence
        precision validator, and the quality-gate identity re-derivation —
        with the sealed bytes untouched throughout.

Data sources (read-only; this script never writes into the runtime
checkout):
  - <runtime-results>/v<N>/logs/master_io.txt         — the real final plan
  - <runtime-results>/v<N>/evidence_snapshot/         — the real frozen bundle
  - <runtime-results>/events.jsonl                    — real v488/v489 events
Sealed ``snapshot_evidence`` bindings are produced by the REAL system
producer (``agent_master_validation._snapshot_reference_evidence_binding``)
against the same frozen snapshot directory the audit bundle loads — the
exact scout-acceptance path.

Exit code 0 iff every POST leg passes.

Usage (the project venv interpreter — web/core imports glicko2):
  /home/ubuntu/pok1/.venv/bin/python scripts/replay_dual_gate_audit_scope.py \
      [--runtime-results /home/ubuntu/pok1/.evolution_pok/web/core/results] \
      [--version N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _fail(message: str) -> None:
    print(f"  [FAIL] {message}")


def _ok(message: str) -> None:
    print(f"  [ ok ] {message}")


# ---------------------------------------------------------------------------
# Real-data loading
# ---------------------------------------------------------------------------


def _pick_generation(runtime_results: Path, version: int | None) -> int:
    if version is not None:
        return int(version)
    candidates = []
    for entry in runtime_results.glob("v*"):
        if not entry.is_dir() or not entry.name[1:].isdigit():
            continue
        if (entry / "evidence_snapshot" / "manifest.json").is_file() and (
            entry / "logs" / "master_io.txt"
        ).is_file():
            candidates.append(int(entry.name[1:]))
    if not candidates:
        raise SystemExit(f"no generation with evidence_snapshot+master_io.txt under {runtime_results}")
    return max(candidates)


def _load_real_plan(runtime_results: Path, next_v: int) -> dict:
    text = (
        runtime_results / f"v{next_v}" / "logs" / "master_io.txt"
    ).read_text(encoding="utf-8", errors="replace")
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for block in reversed(blocks):
        try:
            data = json.loads(block)
        except Exception:
            continue
        if isinstance(data, dict) and "tasks" in data:
            return data
    raise SystemExit(f"no parseable final master plan JSON in v{next_v} master_io.txt")


def _load_real_provenance(runtime_results: Path) -> tuple[str | None, str | None]:
    """The real v489 audit rejection and v488 normalization event excerpts."""
    v489 = v488 = None
    events_path = runtime_results / "events.jsonl"
    if not events_path.is_file():
        return None, None
    try:
        with events_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("type") == "pipeline.master_audit_rejected" and str(
                    (row.get("data") or {}).get("next_v")
                ) == "489" and v489 is None:
                    contradictions = (
                        (row.get("data") or {}).get("audit") or {}
                    ).get("contradictions") or []
                    v489 = "; ".join(str(item) for item in contradictions[:3])
                if (
                    row.get("type") == "pipeline.master_citations_normalized"
                    and str((row.get("data") or {}).get("next_v")) == "489"
                    or (
                        row.get("type") == "pipeline.master_citations_normalized"
                        and v488 is None
                    )
                ):
                    data = row.get("data") or {}
                    if v488 is None:
                        paths = [
                            str(item.get("path"))
                            for item in (data.get("normalization_report") or {}).get(
                                "normalizations"
                            )
                            or []
                        ]
                        v488 = (
                            f"v{data.get('next_v')}: "
                            + "; ".join(paths)
                            if paths
                            else None
                        )
    except Exception:
        return v489, v488
    return v489, v488


def _first_pair_citation(plan_text: str) -> tuple[str, str] | None:
    match = re.search(
        r"(national_cloud_v\d+ vs national_cloud_v\d+): "
        r"games=(\d+), a_wins=(\d+), b_wins=(\d+)",
        plan_text,
    )
    if not match:
        return None
    return match.group(1), match.group(0)


def _aggregate_pointer(plan_text: str) -> str | None:
    match = re.search(
        r"snapshot:(?:bot_stats|selection_snapshot)\.json#[^\s\"'<>|,;)\]]*",
        plan_text,
    )
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# PRE: the pre-audit_scope citable-text view and audit attribution
# ---------------------------------------------------------------------------


def _legacy_flatten(value):
    """Verbatim pre-audit_scope ``_flatten_text`` (web/core/evidence_snapshot.py
    at this change's parent): the sealed proposal structures rendered into
    the audit text.  Kept byte-identical so the PRE reconstruction is the
    exact behavior that rejected v485/v486/v489."""
    if isinstance(value, dict):
        return "\n".join(f"{key}: {_legacy_flatten(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return "\n".join(_legacy_flatten(item) for item in value)
    return str(value or "")


def _legacy_audit_errors(es, plan: dict, next_v: int) -> list[str]:
    """Verbatim pre-audit_scope ``validate_h2h_citations_against_snapshot``
    over the legacy flatten (same helpers, same iteration order; only
    ``text`` differs — the legacy view)."""
    h2h = es.load_generation_h2h_snapshot(next_v)
    if not h2h:
        return []
    text = _legacy_flatten(plan)
    errors: list[str] = []
    for key, row in h2h.items():
        if not isinstance(row, dict):
            continue
        seen_spans: set[tuple[int, int]] = set()
        for alias, first_v, second_v in es._h2h_key_aliases(str(key)):
            if not alias:
                continue
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])",
                re.IGNORECASE,
            )
            for match in pattern.finditer(text):
                span = match.span()
                if span in seen_spans:
                    continue
                seen_spans.add(span)
                window = es._matchup_citation_window(text, span[0], alias)
                cited = {
                    "games": es._extract_int(r"\bgames?\s*[:=]\s*(\d+)", window),
                    "a_wins": es._extract_int(r"\ba_wins\s*[:=]\s*(\d+)", window),
                    "b_wins": es._extract_int(r"\bb_wins\s*[:=]\s*(\d+)", window),
                    "draws": es._extract_int(r"\bdraws\s*[:=]\s*(\d+)", window),
                }
                if cited["games"] is None:
                    cited["games"] = es._extract_int(
                        r"(?<![\w.])(\d+)\s*(?:g|games|局)\b", window
                    )
                wl_match = es._WL_RE.search(window)
                if wl_match:
                    wins = int(wl_match.group(1))
                    losses = int(wl_match.group(2))
                    cited["games"] = (
                        cited["games"] if cited["games"] is not None else wins + losses
                    )
                    key_match = es._h2h_key_re().search(str(key))
                    key_a = key_match.group(1) if key_match else first_v
                    if first_v == key_a:
                        cited["a_wins"] = (
                            cited["a_wins"] if cited["a_wins"] is not None else wins
                        )
                        cited["b_wins"] = (
                            cited["b_wins"] if cited["b_wins"] is not None else losses
                        )
                    else:
                        cited["a_wins"] = (
                            cited["a_wins"] if cited["a_wins"] is not None else losses
                        )
                        cited["b_wins"] = (
                            cited["b_wins"] if cited["b_wins"] is not None else wins
                        )
                for field, value in cited.items():
                    if value is None:
                        continue
                    actual = int(row.get(field, 0) or 0)
                    if value != actual:
                        errors.append(
                            f"{alias} cited {field}={value}, snapshot has "
                            f"{field}={actual} (key {key})"
                        )
    return errors


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-results",
        default="/home/ubuntu/pok1/.evolution_pok/web/core/results",
        help="runtime checkout results dir (read-only data source)",
    )
    parser.add_argument(
        "--version",
        type=int,
        default=None,
        help="generation version to replay (default: newest with real artifacts)",
    )
    args = parser.parse_args()

    runtime_results = Path(args.runtime_results)
    # The runtime data source is the cloud evolution line
    # (national_cloud_v namespace, tencent-cloud-runtime branch).  Seed the
    # same env the service runs under so bot_namespace resolves the same
    # ACTIVE_BOT_PREFIX the snapshot rows were published with.
    import os

    os.environ.setdefault("POK_CLOUD_RUNTIME", "1")
    sys.path.insert(0, str(REPO_ROOT / "web" / "core"))

    import evolution_infra

    evolution_infra.RESULTS_DIR = runtime_results

    import evidence_snapshot as es
    from agent_master_validation import (
        _proposal_identity,
        _snapshot_reference_evidence_binding,
    )
    from bot_artifact import canonical_digest

    next_v = _pick_generation(runtime_results, args.version)
    print(f"dual-gate audit_scope replay — generation v{next_v} (real products)")
    bundle = es.load_generation_evaluation_snapshot(next_v)
    if not isinstance(bundle, dict) or not bundle.get("available"):
        raise SystemExit(
            f"real frozen snapshot for v{next_v} failed to load: "
            f"{bundle.get('reason') if isinstance(bundle, dict) else bundle}"
        )
    print(f"frozen snapshot bundle loaded: {len(bundle.get('h2h') or {})} H2H rows")

    # --- real provenance -------------------------------------------------
    v489_excerpt, v488_excerpt = _load_real_provenance(runtime_results)
    if v489_excerpt:
        print(f"\nreal v489 rejection (provenance): {v489_excerpt[:220]}")
    else:
        print("\nreal v489 rejection event not found in events.jsonl (informational)")
    if v488_excerpt:
        print(f"real sealed-rewrite event (provenance): {v488_excerpt[:220]}")

    # --- real plan + real sealed structures ------------------------------
    plan = _load_real_plan(runtime_results, next_v)
    plan_text = json.dumps(plan, ensure_ascii=False)
    pair = _first_pair_citation(plan_text)
    if pair is None:
        raise SystemExit("real plan carries no canonical pair citation to bind")
    pair_key, pair_citation = pair
    snapshot_dir = runtime_results / f"v{next_v}" / "evidence_snapshot"
    h2h = es.load_generation_h2h_snapshot(next_v)
    if pair_key not in h2h:
        raise SystemExit(f"plan pair {pair_key} absent from the frozen v{next_v} snapshot")

    pair_binding = _snapshot_reference_evidence_binding(
        f"snapshot:head_to_head.json#/{pair_key}", snapshot_dir
    )
    aggregate_pointer = _aggregate_pointer(plan_text) or (
        "snapshot:selection_snapshot.json#/rows"
    )
    aggregate_binding = _snapshot_reference_evidence_binding(
        aggregate_pointer, snapshot_dir
    )
    if pair_binding is None or aggregate_binding is None:
        raise SystemExit(
            "real producer could not bind the real references against the "
            f"real snapshot (pair={pair_binding is not None}, "
            f"aggregate={aggregate_binding is not None})"
        )
    print(
        f"\nreal plan citation: {pair_citation}\n"
        f"sealed pair binding   : games={pair_binding['games']} "
        f"a_wins={pair_binding.get('a_wins')} b_wins={pair_binding.get('b_wins')}\n"
        f"sealed aggregate bind : {aggregate_binding['reference']} "
        f"games={aggregate_binding['games']}"
    )

    proposal = json.loads(
        json.dumps(
            {
                "schema_version": "master-proposal-v4",
                "change_symbol": "policy.py:_polarized_raise_fraction",
                "snapshot_evidence": [pair_binding, aggregate_binding],
            },
            sort_keys=True,
        )
    )
    proposal["proposal_id"] = _proposal_identity(proposal)
    sealed_role_result_digest = canonical_digest(proposal)

    def _assemble(base_plan: dict) -> dict:
        import copy

        assembled = copy.deepcopy(base_plan)
        assembled["selected_proposal_id"] = proposal["proposal_id"]
        assembled["proposal_binding"] = json.loads(
            json.dumps(
                {
                    "selected_proposal_id": proposal["proposal_id"],
                    "snapshot_evidence": [pair_binding, aggregate_binding],
                },
                sort_keys=True,
            )
        )
        assembled["proposal_ensemble"] = {"ordered_proposals": [proposal]}
        return assembled

    replay_plan = _assemble(plan)

    # --- PRE A: false rejection over the legacy citable-text view --------
    print("\nPRE A — legacy audit view (sealed bytes flattened into the text):")
    legacy_errors = _legacy_audit_errors(es, replay_plan, next_v)
    window_errors = [
        error
        for error in legacy_errors
        if f"cited games={aggregate_binding['games']}" in error
    ]
    if window_errors:
        _ok(
            "audit REJECTS the correct plan — pair window swallowed the sealed "
            f"aggregate games leaf ({len(window_errors)} occurrence(s), the real "
            "v489 class):"
        )
        for error in window_errors[:2]:
            print(f"         {error}")
    else:
        _fail("legacy audit did not reproduce the sealed-window misattribution")
        return 2
    if legacy_errors == window_errors:
        _ok("every legacy rejection is a sealed-window false attribution "
            "(the plan's own text is snapshot-exact)")
    else:
        _fail(f"legacy audit found prose errors in a snapshot-exact plan: "
              f"{[e for e in legacy_errors if e not in window_errors][:3]}")
        return 2

    # --- PRE A2: v489 wrong-prose repaired by the seam, still rejected ----
    print("\nPRE A2 — real v489 wrong-prose numbers, seam-repaired, still rejected:")
    wrong_row = h2h.get("national_cloud_v11 vs national_cloud_v27") or next(
        (row for row in h2h.values() if isinstance(row, dict) and row.get("games")),
        None,
    )
    if isinstance(wrong_row, dict):
        # The real v489 pattern: the plan's own text quoted a DIFFERENT
        # row's numbers against the pair alias (v185-vs-v27's 33/21/12 on
        # the v11-vs-v27 alias).  Plant that on the real pair.
        v489_plan = json.loads(json.dumps(plan))
        wrong_citation = (
            f"{pair_key}: games=33, a_wins=21, b_wins=12, draws=0."
        )
        v489_plan["analysis"] = wrong_citation + " " + str(v489_plan.get("analysis") or "")
        v489_plan["targeted_failure"] = wrong_citation
        v489_sealed = _assemble(v489_plan)
        report = es.normalize_master_plan_citations(v489_sealed, next_v)
        repaired = wrong_citation.replace(
            "games=33", f"games={pair_binding['games']}"
        ).replace("a_wins=21", f"a_wins={pair_binding.get('a_wins')}").replace(
            "b_wins=12", f"b_wins={pair_binding.get('b_wins')}"
        )
        if repaired in v489_sealed["targeted_failure"] and report["total"] >= 3:
            _ok(f"normalization seam repaired the wrong prose in place "
                f"({report['total']} rewrites)")
        else:
            _fail("normalization seam did not repair the planted wrong prose")
            return 2
        still = _legacy_audit_errors(es, v489_sealed, next_v)
        if any(f"cited games={aggregate_binding['games']}" in e for e in still):
            _ok("legacy audit STILL rejects after prose repair — the v489 death")
        else:
            _fail("legacy audit unexpectedly passed the repaired plan")
            return 2

    # --- PRE B: the v488 re_seal temptation -------------------------------
    print("\nPRE B — re_seal temptation (v488): rewriting a sealed games leaf:")
    import copy

    resealed = copy.deepcopy(replay_plan)
    resealed["proposal_ensemble"]["ordered_proposals"][0]["snapshot_evidence"][1][
        "games"
    ] = pair_binding["games"]
    live = resealed["proposal_ensemble"]["ordered_proposals"][0]
    identity_ok = _proposal_identity(live) == live["proposal_id"]
    digest_ok = canonical_digest(live) == sealed_role_result_digest
    if identity_ok or digest_ok:
        _fail("sealed rewrite did not break the quality-gate identity re-derivation")
        return 2
    _ok("quality gate identity broken: proposal_id mismatch AND scout "
        "role_result_digest can never be recomputed")
    precision_on_resealed = es.validate_sealed_proposal_evidence_precision(
        resealed, next_v
    )
    if any("scalar_mismatch" in e for e in precision_on_resealed):
        _ok("sealed-evidence precision also flags the rewrite "
            f"({precision_on_resealed[0][:120]})")
    else:
        _fail("precision validator did not flag the resealed binding")
        return 2

    # --- POST: fixed tree, live functions ---------------------------------
    print("\nPOST — fixed tree (live web/core functions, sealed bytes untouched):")
    post_ok = True

    accuracy = es.validate_h2h_citations_against_snapshot(replay_plan, next_v)
    if accuracy == []:
        _ok("prose-citation accuracy audit passes (sealed subtrees excluded)")
    else:
        _fail(f"accuracy audit failed: {accuracy[:3]}")
        post_ok = False

    floor = es.statistical_evidence_floor_errors(replay_plan, next_v)
    if floor == []:
        _ok("two-tier statistical floor passes (aggregate leg via structured citation)")
    else:
        _fail(f"statistical floor failed: {floor[:2]}")
        post_ok = False

    precision = es.validate_sealed_proposal_evidence_precision(replay_plan, next_v)
    if precision == []:
        _ok("sealed-evidence byte-level precision passes (pointer+node_sha256+"
            "scalars+projection reconcile)")
    else:
        _fail(f"sealed-evidence precision failed: {precision[:3]}")
        post_ok = False

    live = replay_plan["proposal_ensemble"]["ordered_proposals"][0]
    if _proposal_identity(live) == live["proposal_id"]:
        _ok("quality-gate identity re-derivation matches (proposal_id)")
    else:
        _fail("quality-gate identity mismatch on untouched sealed bytes")
        post_ok = False
    if canonical_digest(live) == sealed_role_result_digest:
        _ok("scout role_result_digest re-derivation matches")
    else:
        _fail("scout role_result_digest mismatch on untouched sealed bytes")
        post_ok = False

    marked, _end = es._flatten_marked(replay_plan, [], 0)
    flatten_identity = marked == es._flatten_text(replay_plan)
    sealed_digest_absent = (
        pair_binding["node_sha256"] not in marked
        and aggregate_binding["node_sha256"] not in marked
        and aggregate_binding["resolved_projection"][:80] not in marked
    )
    if flatten_identity and sealed_digest_absent:
        _ok("shared citable-text view: _flatten_marked == _flatten_text and "
            "sealed bytes do not render")
    else:
        _fail(
            f"citable-text view broken (identity={flatten_identity}, "
            f"sealed_absent={sealed_digest_absent})"
        )
        post_ok = False

    before = json.dumps(replay_plan, sort_keys=True, separators=(",", ":"))
    report = es.normalize_master_plan_citations(replay_plan, next_v)
    unchanged = (
        json.dumps(replay_plan, sort_keys=True, separators=(",", ":")) == before
    )
    if unchanged and report["total"] == 0:
        _ok("normalization is a no-op on the snapshot-exact plan; sealed bytes "
            "byte-exact throughout")
    else:
        _fail(f"normalization mutated the plan ({report['total']} rewrites, "
              f"unchanged={unchanged})")
        post_ok = False

    print()
    if post_ok:
        print(f"POST all green — dual-gate replay PASSED for v{next_v}")
        return 0
    print("POST failed — dual-gate replay FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
