"""First-layer false-rejection fixes (audit 2026-10-04, P2-P5).

Four deterministic validator gates rejected factually correct Master work:

* P2 — ``output_schema.STATE_LEARNING_INTERVENTION_TARGET_ALIASES`` for
  ``opponent.showdown_range`` lagged the runtime schema: the tracker
  (``bots/national_cloud_v88/national_bot.py:815-831``) publishes
  ``selection_scope`` / ``selection_bias_guard`` / ``bucket_priors`` /
  ``bucket_counts`` / ``bucket_rates``, ``policy.py:133-136`` consumes
  ``bucket_rates`` / ``bucket_priors``, and ``strategy_reference_pack.py``
  lists three of them as ``required_decision_context_fields`` — so a Scout
  that restated the published field set was rejected with
  ``proposal_mechanism_root_scoped_unknown_leaf``.
* P3 — the same snapshot file reaches the prompt in two path forms (the
  repo-relative ``h2h_relpath`` line and the ``snapshot:`` pointer form);
  a model that merged them into a repo-relative reference failed
  ``_validated_snapshot_reference`` and cascaded into
  ``evidence_ref_invalid`` + ``snapshot_evidence_required`` + ``cited.0``.
* P4 — the measurement contract compared the ``samples`` literal
  ``">=30_complete_matches"`` with ``==``; every natural spelling variant
  (``>=30``, ``30_complete_matches``, spaced forms) was rejected.
* P5 — the deterministic citation audit chain shared one broad ``except``
  that blanked the error list, so an audit crash was indistinguishable
  from an audit pass (fail-open).
"""

import json
import sys
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


# --- P2: showdown_range whitelist must cover the runtime-published leaves ---


def test_showdown_range_whitelist_covers_runtime_published_leaves():
    """The root-scoped closed-leaf whitelist must mirror what the runtime
    actually publishes under ``opponent.showdown_range`` (v88 tracker:
    selection_scope/selection_bias_guard/bucket_priors/bucket_counts/
    bucket_rates), not a stale narrower subset."""
    import output_schema as os_

    aliases = os_.STATE_LEARNING_INTERVENTION_TARGET_ALIASES[
        "opponent.showdown_range"
    ]
    for leaf in (
        "selection_scope",
        "selection_bias_guard",
        "bucket_priors",
        "bucket_counts",
        "bucket_rates",
    ):
        qualified = f"opponent.showdown_range.{leaf}"
        assert qualified in aliases, (
            f"{qualified} is published by the native tracker and consumed by "
            "policy.py, but is missing from the root-scoped whitelist — a "
            "proposal restating the runtime schema is falsely rejected "
            "(proposal_mechanism_root_scoped_unknown_leaf)"
        )


def test_reference_pack_required_fields_covered_by_intervention_whitelist():
    """Anti-drift: every root-scoped ``opponent.*`` field the reference pack
    declares as required must already be a whitelisted child of its root, so
    a future card that names a new runtime field without syncing
    ``STATE_LEARNING_INTERVENTION_TARGET_ALIASES`` turns this test red."""
    import output_schema as os_
    import strategy_reference_pack as srp

    whitelist = {
        root: set(aliases)
        for root, aliases in os_.STATE_LEARNING_INTERVENTION_TARGET_ALIASES.items()
    }
    roots = sorted(whitelist, key=len, reverse=True)

    def owning_root(field: str):
        for root in roots:
            if field.startswith(root + "."):
                return root
        return None

    missing = []
    for card in srp._CARDS:
        declared = tuple(card.required_decision_context_fields) + tuple(
            card.required_any_decision_context_fields
        )
        for field in declared:
            field = str(field).strip()
            if not field.startswith("opponent."):
                continue
            root = owning_root(field)
            # Top-level opponent scalars (root "opponent") are not governed by
            # any root-scoped intervention whitelist; only children of a
            # governed root are in scope here.
            if root is None:
                continue
            if field not in whitelist[root]:
                missing.append(f"{field} (card {card.reference_id})")
    assert not missing, (
        "Reference-pack required decision_context fields are not covered by "
        "STATE_LEARNING_INTERVENTION_TARGET_ALIASES — a Scout following the "
        "card would be falsely rejected: " + "; ".join(sorted(set(missing)))
    )


def test_root_scoped_list_accepts_runtime_showdown_leaves():
    """Behavioral guard: a root-scoped list naming exactly the five
    runtime-published showdown leaves must not raise
    proposal_mechanism_root_scoped_unknown_leaf."""
    import agent_master_proposal_primaries as pp

    proposal = {
        "mechanism_target": "opponent.showdown_range",
        "structural_change": (
            "Rebucket the capped showdown posterior through the tracker's "
            "published guard fields: opponent.showdown_range (selection_scope, "
            "selection_bias_guard, bucket_priors, bucket_counts, bucket_rates) "
            "stay the only touched inputs."
        ),
        "expected_diff": (
            "opponent.showdown_range (selection_scope, bucket_rates) remain "
            "byte-identical selection metadata; only the capped bucket "
            "multiplier weight changes."
        ),
    }
    falsifier = {
        "test_name": "showdown_range_adaptation",
        "intervention_target": "opponent.showdown_range",
        "intervention": (
            "Vary only the showdown bucket weight; every other "
            "decision_context field stays byte-identical."
        ),
    }
    errors = pp._proposal_mechanism_target_errors(proposal, falsifier)
    unknown_leaf = [
        error
        for error in errors
        if error.startswith("proposal_mechanism_root_scoped_unknown_leaf")
    ]
    assert not unknown_leaf, (
        "The root-scoped list names only runtime-published leaves but was "
        "rejected: " + ",".join(unknown_leaf)
    )


# --- P3: snapshot reference must parse both prompt-rendered path forms ------


def _make_snapshot_dir(tmp_path):
    snap = tmp_path / "evidence_snapshot"
    snap.mkdir(exist_ok=True)
    (snap / "head_to_head.json").write_text(
        json.dumps(
            {
                "national_cloud_v1 vs national_cloud_v2": {
                    "games": 45,
                    "a_wins": 20,
                    "b_wins": 25,
                    "draws": 0,
                    "win_rate": 0.4444,
                }
            }
        ),
        encoding="utf-8",
    )
    return snap


def test_validated_snapshot_reference_accepts_repo_relative_form(tmp_path):
    """The prompt renders the snapshot file both as a repo-relative path
    (``h2h_snapshot_contract_text``'s ``- Snapshot file:`` line) and as a
    ``snapshot:`` pointer.  A reference written in the repo-relative form
    must normalize to the exact same canonical pointer instead of failing."""
    import agent_master_validation as amv

    snap = _make_snapshot_dir(tmp_path)
    locator = "#/national_cloud_v1 vs national_cloud_v2"
    canonical = amv._validated_snapshot_reference(
        f"snapshot:head_to_head.json{locator}", snap
    )
    assert canonical == f"snapshot:head_to_head.json{locator}"

    repo_relative = amv._validated_snapshot_reference(
        "web/core/results/v1/evidence_snapshot/head_to_head.json" + locator,
        snap,
    )
    assert repo_relative == canonical, (
        "A repo-relative snapshot reference (the exact path form the prompt "
        "renders via h2h_relpath) must resolve to the same canonical pointer; "
        "rejecting it cascades into evidence_ref_invalid + "
        "snapshot_evidence_required"
    )


def test_snapshot_binding_accepts_repo_relative_form(tmp_path):
    """The structured binding (games/node_sha256/projection) must also accept
    the repo-relative form and report the canonical pointer back."""
    import agent_master_validation as amv

    snap = _make_snapshot_dir(tmp_path)
    binding = amv._snapshot_reference_evidence_binding(
        "web/core/results/v1/evidence_snapshot/head_to_head.json"
        "#/national_cloud_v1 vs national_cloud_v2",
        snap,
    )
    assert binding is not None, (
        "The repo-relative reference form must produce a binding, not None"
    )
    assert binding["games"] == 45
    assert binding["reference"] == (
        "snapshot:head_to_head.json#/national_cloud_v1 vs national_cloud_v2"
    )


def test_repo_relative_snapshot_ref_no_longer_cascades(tmp_path):
    """End-to-end over the projection-hints gate: an evidence_refs entry in
    the repo-relative path form must count as the snapshot citation, so the
    proposal is no longer rejected with evidence_ref_invalid +
    snapshot_evidence_required."""
    import agent_master_validation as amv

    snap = _make_snapshot_dir(tmp_path)
    # An aggregate row so the two-tier bar can pass alongside the matchup row.
    (snap / "bot_stats.json").write_text(
        json.dumps(
            {
                "national_cloud_v1": {
                    "games": 230,
                    "a_wins": 110,
                    "b_wins": 120,
                    "draws": 0,
                    "win_rate": 0.478,
                }
            }
        ),
        encoding="utf-8",
    )
    proposal = {
        "schema_version": "2",
        "direction": "showdown_range_adaptation",
        "mechanism_target": "opponent.showdown_range",
        "targeted_failure": (
            "The capped showdown posterior misweights tight buckets against "
            "the observed reached-showdown population."
        ),
        "structural_change": (
            "Reweight the showdown bucket multiplier using only "
            "opponent.showdown_range fields; every other decision_context "
            "field stays byte-identical."
        ),
        "counterfactual": (
            "Without the reweight the bucket multiplier stays at the prior "
            "and the tight-bucket exploit remains uncorrected."
        ),
        "measurement": (
            "target=national_cloud_v1; primary=complete_70_hand_wld; "
            "expected_delta=0.03; samples=>=30_complete_matches; "
            "uncertainty=wilson_wld_interval; secondary=net_chip_ci"
        ),
        "why_not_threshold_tuning": (
            "Threshold tuning cannot correct a distributional posterior "
            "weight; the fix is bounded per-bucket reweighting."
        ),
        "expected_diff": (
            "opponent.showdown_range bucket multiplier clamps change from "
            "0.50..1.75 to 0.60..1.60 with identical priors."
        ),
        "target_files": ["policy.py"],
        "source_symbols": ["policy.py:get_baseline_decision"],
        "change_symbol": "policy.py:get_baseline_decision",
        "reachable_chain": [
            "policy.py:iter_decisions",
            "policy.py:get_baseline_decision",
        ],
        "falsifier": {
            "test_name": "showdown_range_adaptation",
            "state_learning_primary": "showdown_range",
            "intervention_target": "opponent.showdown_range",
            "control": (
                "Prior-weighted bucket multipliers without the observed "
                "showdown counts."
            ),
            "intervention": (
                "Reweight opponent.showdown_range bucket multipliers; every "
                "other decision_context field stays byte-identical."
            ),
            "expected_observation": (
                "Tight-bucket bluff-catch frequency shifts by at least two "
                "percentage points against the tight opponent."
            ),
        },
        "evidence_refs": [
            "source:policy.py:get_baseline_decision",
            "web/core/results/v1/evidence_snapshot/head_to_head.json"
            "#/national_cloud_v1 vs national_cloud_v2",
            "snapshot:bot_stats.json#/national_cloud_v1",
        ],
        "risks": (
            "Sparse showdown samples keep the multiplier near the prior, so "
            "the expected effect may not materialize within one generation."
        ),
    }
    hints = amv._master_proposal_projection_hints(
        json.dumps(proposal),
        source_graph={
            "policy.py:iter_decisions": {"policy.py:get_baseline_decision"},
            "policy.py:get_baseline_decision": set(),
        },
        snapshot_dir=snap,
        national_policy_only=True,
        require_snapshot_evidence=True,
        evidence_mode="frozen_strength_snapshot",
    )
    assert "proposal_evidence_ref_invalid" not in hints, (
        "The repo-relative snapshot reference form was rejected as an invalid "
        f"evidence ref; hints were: {hints}"
    )
    assert "proposal_snapshot_evidence_required" not in hints, (
        "The repo-relative snapshot reference did not count as the required "
        f"snapshot citation; hints were: {hints}"
    )


def test_two_tier_token_distinguishes_unresolved_references():
    """``cited.0`` must distinguish "no snapshot reference written at all"
    from "references were written but none resolved": the repair hint for
    the latter must point at pointer syntax, not at adding a citation."""
    import agent_master_validation as amv

    legacy = amv._snapshot_evidence_two_tier_errors([])
    assert legacy == [
        "proposal_cited_sample_too_small.matchup.none"
        ".cited.0.best_available.0.tier.30"
        ".and_aggregate.200"
        ".aggregate_sources.bot_stats.selection_snapshot"
    ], "the unwritten form must stay byte-identical for audit-mirror callers"

    unresolved = amv._snapshot_evidence_two_tier_errors(
        [], None, written_reference_count=2
    )
    assert unresolved and ".refs_written.2." in unresolved[0], (
        "when snapshot-shaped references were written but none resolved, the "
        "rejection token must say how many were written so the repair hint "
        f"points at pointer resolution, got: {unresolved}"
    )


def test_repair_guidance_directs_unresolved_references_to_pointer_syntax():
    """The schema-repair prompt for the unresolved form must tell the model
    its written references failed to resolve (copy an exact pointer), not
    merely repeat the generic 'add a citation' advice."""
    import agent_master_validation as amv

    unresolved = amv._snapshot_evidence_two_tier_errors(
        [], None, written_reference_count=2
    )
    assert unresolved
    guidance = amv._proposal_schema_repair_guidance(
        tuple(unresolved),
        require_snapshot_evidence=True,
    )
    assert "resolve" in guidance.lower(), (
        "the repair guidance must explicitly address unresolved snapshot "
        f"references; got: {guidance!r}"
    )


# --- P4: measurement samples literal must accept equivalent spellings -------


@pytest.mark.parametrize(
    "samples",
    [
        ">=30_complete_matches",  # canonical
        ">=30",
        "30_complete_matches",
        ">=30 complete matches",
        "30 complete matches",
        ">=30_complete matches",
        "30_completeMatches".lower(),
        "≥30_complete_matches",
        "at_least_30_complete_matches",
        ">= 30_complete_matches",
        ">=30completematches",
    ],
)
def test_measurement_samples_floor_accepts_equivalent_spellings(samples):
    """Semantically equivalent spellings of the sample floor must pass the
    measurement contract (the model repeatedly rewrote the literal and every
    Scout + retry burned on the == comparison)."""
    import agent_master_validation as amv

    measurement = (
        "target=national_cloud_v1; primary=complete_70_hand_wld; "
        f"expected_delta=0.03; samples={samples}; "
        "uncertainty=wilson_wld_interval; secondary=net_chip_ci"
    )
    assert amv._proposal_measurement_contract_valid(
        measurement, "frozen_strength_snapshot"
    ), f"equivalent samples spelling rejected: {samples!r}"


@pytest.mark.parametrize(
    "samples",
    [
        ">30_complete_matches",  # strict >, different predicate
        ">=29_complete_matches",  # below the floor
        "30",  # bare count is not the >= floor contract
        ">=30_incomplete_matches",
        ">=300_complete_matches",  # not equivalent (different floor)
        "",
    ],
)
def test_measurement_samples_floor_still_rejects_non_equivalent(samples):
    """The tolerance is equivalence, not relaxation: a different predicate,
    a lower floor, or an unrelated literal still fails."""
    import agent_master_validation as amv

    measurement = (
        "target=national_cloud_v1; primary=complete_70_hand_wld; "
        f"expected_delta=0.03; samples={samples}; "
        "uncertainty=wilson_wld_interval; secondary=net_chip_ci"
    )
    assert not amv._proposal_measurement_contract_valid(
        measurement, "frozen_strength_snapshot"
    ), f"non-equivalent samples spelling accepted: {samples!r}"


def test_measurement_repair_guidance_renders_unambiguous_floor_literal():
    """The repair hint must render the samples literal in an unambiguous
    fenced form so the retry copies it character-for-character."""
    import agent_master_validation as amv

    guidance = amv._proposal_schema_repair_guidance(
        ("proposal_measurement_contract_invalid",),
        require_snapshot_evidence=True,
    )
    assert "`>=30_complete_matches`" in guidance, (
        "the repair guidance must fence the exact samples literal so the "
        f"retry cannot reinterpret it; got: {guidance!r}"
    )


# --- P5: citation audit must fail closed on internal exceptions -------------


def test_citation_audit_exception_fails_closed(monkeypatch, tmp_path):
    """An internal exception inside the deterministic citation audit chain
    must surface as an explicit fail-closed rejection token, never as an
    empty error list (which the dispatch treats as audit-passed)."""
    import evidence_snapshot as es
    import tool_planning as tp
    import tool_planning_master_dispatch as dispatch

    def _boom(*args, **kwargs):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(
        es, "validate_h2h_citations_against_snapshot", _boom
    )
    events = []
    monkeypatch.setattr(
        tp, "log_system_event", lambda *a, **k: events.append(a)
    )
    errors, guidance = dispatch._collect_h2h_citation_audit(
        {"worker_prompt": "plan"}, 42, 41
    )
    assert errors == ["h2h_citation_audit_failed_closed:RuntimeError"], (
        "an audit exception must block the audit (fail-closed), not blank "
        f"the error list; got: {errors!r}"
    )
    assert guidance == ""
    assert events, "the exception must be recorded as an explicit event"


def test_master_dispatch_citation_audit_is_not_fail_open():
    """Static guard: run_master_impl must route the citation audit through
    the fail-closed collector and must not contain the historical
    exception-blanking form (the bootstrap no-strength branch legitimately
    starts from an empty list — that is a mode, not an exception swallow)."""
    import re

    src = (CORE_DIR / "tool_planning_master_dispatch.py").read_text(
        encoding="utf-8"
    )
    assert "_collect_h2h_citation_audit(" in src, (
        "the citation audit chain must run through the fail-closed collector"
    )
    assert "h2h_citation_audit_failed_closed" in src, (
        "the fail-closed rejection token must exist in the dispatch module"
    )
    fail_open = re.search(
        r"except\s+Exception[^:]*:\s*\n\s*_h2h_citation_errors\s*=\s*\[\]",
        src,
    )
    assert fail_open is None, (
        "the fail-open except that blanked the citation error list must not "
        "return"
    )
