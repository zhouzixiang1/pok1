"""Evolution-layer fix batch (2026-08-16 approved plan).

1.1 source-history parser: native-tier publications write ``source: vN`` in
the commit body (not the legacy ``parent: national_cloud_vN``), and
``parse_bot_version`` rejects bare ``vN`` — so every parent read returned
None/non-int, ``_read_source_v_history`` returned [] (one bootstrap string
parent aborted the whole list), and the loop/oscillation detectors were
blind from birth.
"""

import json
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import evolution_infra_git_publication as gitpub  # noqa: E402


def _fake_git(tag, commit, body):
    def _git(*args, check=False):
        name = args[0]
        if name == "tag":
            return tag
        if name == "rev-list":
            return commit
        if name == "show":
            return body
        return ""

    return _git


def test_git_get_parent_reads_source_prefix_and_bare_v(monkeypatch):
    monkeypatch.setattr(gitpub, "bot_tag", lambda v: f"national-cloud-bot-v{v}")
    monkeypatch.setattr(
        gitpub._ei, "_git",
        _fake_git("x", "a" * 40, "National bot v186: master\n\nsource: v1\n"),
    )
    assert gitpub.git_get_parent(186) == 1

    monkeypatch.setattr(
        gitpub._ei, "_git",
        _fake_git("x", "a" * 40, "title\nsource: v185\n"),
    )
    assert gitpub.git_get_parent(186) == 185


def test_git_get_parent_still_reads_legacy_parent_and_ignores_parent2(monkeypatch):
    monkeypatch.setattr(gitpub, "bot_tag", lambda v: f"national-cloud-bot-v{v}")
    monkeypatch.setattr(
        gitpub._ei, "_git",
        _fake_git("x", "a" * 40, "evolve: v1 -> v11\n\nparent: national_cloud_v1\n"),
    )
    assert gitpub.git_get_parent(11) == 1

    # parent2 must NEVER match (crossover second parent is not the lineage).
    monkeypatch.setattr(
        gitpub._ei, "_git",
        _fake_git("x", "a" * 40, "title\nparent2: v3\nsource: v27\n"),
    )
    assert gitpub.git_get_parent(29) == 27


def test_git_get_parent_no_marker_returns_none(monkeypatch):
    monkeypatch.setattr(gitpub, "bot_tag", lambda v: f"national-cloud-bot-v{v}")
    monkeypatch.setattr(
        gitpub._ei, "_git", _fake_git("", "", "")
    )
    assert gitpub.git_get_parent(999) is None


def test_read_source_v_history_skips_unparseable_entries(monkeypatch):
    import generation_scheduler_source_selection as ss
    import national_runtime_authority as nra
    import evolution_infra as ei

    monkeypatch.setattr(
        nra, "strict_published_bot_names",
        lambda: ["national_cloud_v1", "national_cloud_v11", "national_cloud_v173"],
    )
    fake_parents = {1: "national_cloud_v0", 11: 1, 173: 83}
    monkeypatch.setattr(
        ei, "git_get_parent", lambda v: fake_parents.get(v), raising=False
    )
    # ss imports git_get_parent from evolution_infra at call time.
    history = ss._read_source_v_history()
    assert history == [1, 83]


# --- 1.2 statistical evidence bar (two-tier) ---------------------------------


def test_two_tier_hint_is_compact_and_charset_safe():
    import re

    import agent_master_validation as amv

    # Neither tier met.
    errs = amv._snapshot_evidence_two_tier_errors([12, 7])
    assert errs == [
        "proposal_cited_sample_too_small.max_games_seen.12"
        ".need_primary.30.and_aggregate.200"
        ".aggregate_sources.bot_stats.selection_snapshot"
    ]
    for err in errs:
        assert len(err) <= 160
        assert re.fullmatch(r"[a-z0-9_.:-]+", err) is not None

    # Primary met, aggregate missing.
    assert amv._snapshot_evidence_two_tier_errors([45])
    # Both met (a single 200+ row satisfies both tiers).
    assert amv._snapshot_evidence_two_tier_errors([45, 234]) == []
    assert amv._snapshot_evidence_two_tier_errors([]) 


def test_binding_exposes_structured_games_scalars(tmp_path, monkeypatch):
    import agent_master_validation as amv

    snap = tmp_path / "snapshot"
    snap.mkdir()
    (snap / "head_to_head.json").write_text(
        json.dumps(
            {"national_cloud_v1 vs national_cloud_v2": {
                "games": 45, "a_wins": 20, "b_wins": 25, "draws": 0,
                "win_rate": 0.4444,
            }}
        ),
        encoding="utf-8",
    )
    binding = amv._snapshot_reference_evidence_binding(
        "snapshot:head_to_head.json#/national_cloud_v1 vs national_cloud_v2",
        snap,
    )
    assert binding is not None
    assert binding["games"] == 45
    assert binding["a_wins"] == 20
    assert binding["b_wins"] == 25


def test_audit_floor_errors_require_primary_and_aggregate(monkeypatch, tmp_path):
    import evidence_snapshot as es

    monkeypatch.setattr(
        es,
        "load_generation_h2h_snapshot",
        lambda next_v: {
            "national_cloud_v1 vs national_cloud_v185": {
                "games": 55, "a_wins": 27, "b_wins": 28, "draws": 0,
                "win_rate": 0.5,
            },
            "national_cloud_v1 vs national_cloud_v29": {
                "games": 8, "a_wins": 3, "b_wins": 5, "draws": 0,
                "win_rate": 0.4,
            },
        },
    )
    # Primary met (55>=30) but no aggregate snapshot reference in the text.
    errs = es.statistical_evidence_floor_errors(
        {"worker_prompt": "national_cloud_v1 vs national_cloud_v185 games=55 weakness"},
        190,
    )
    assert errs and "aggregate corroboration" in errs[0]

    # With an aggregate snapshot reference, the bar passes.
    errs = es.statistical_evidence_floor_errors(
        {
            "worker_prompt": "national_cloud_v1 vs national_cloud_v185 games=55 weakness",
            "evidence_refs": ["snapshot:bot_stats.json#/national_cloud_v1"],
        },
        190,
    )
    assert errs == []

    # Only a sparse row cited: primary tier fails too.
    errs = es.statistical_evidence_floor_errors(
        {
            "worker_prompt": "national_cloud_v1 vs national_cloud_v29 games=8 weakness",
            "evidence_refs": ["snapshot:bot_stats.json#/national_cloud_v1"],
        },
        190,
    )
    assert errs and "primary matchup row games >= 30" in errs[0]


# --- 1.3 direction dedup / retry pinning / direction-scoped novelty --------


def test_extract_change_symbol_from_output():
    import agent_master_ensemble as ens

    raw = (
        'noise {"change_symbol": "policy.py:_first"} noise '
        '{"change_symbol": "policy.py:_final"}'
    )
    assert ens._extract_change_symbol_from_output(raw) == "policy.py:_final"
    assert ens._extract_change_symbol_from_output("") is None
    assert ens._extract_change_symbol_from_output(None) is None


def test_recent_directions_module_shared_ledger(tmp_path):
    import recent_directions as rd

    for v, symbol in ((187, "_refinement_prior_equity"), (186, "_bluff_allowed")):
        log_dir = tmp_path / f"v{v}" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "master_io.txt").write_text(
            f'{{"change_symbol": "policy.py:{symbol}"}}', encoding="utf-8"
        )
    rows = rd.recent_change_symbols(results_dir=tmp_path)
    assert rows == [(187, "policy.py:_refinement_prior_equity"), (186, "policy.py:_bluff_allowed")]
    counts = rd.recent_symbol_counts(results_dir=tmp_path)
    assert counts == {
        "policy.py:_refinement_prior_equity": 1,
        "policy.py:_bluff_allowed": 1,
    }
    assert rd.recent_change_symbols(results_dir=tmp_path / "none") == []


def test_packet_backstop_rejects_duplicate_change_symbols():
    import json as _json

    import agent_master_proposal_packet as packet

    def row(pid):
        return {
            "schema_version": 2, "direction": "mechanism",
            "proposal_id": pid, "change_symbol": "policy.py:_same",
            "targeted_failure": "x" * 30, "structural_change": "y" * 30,
            "counterfactual": "z" * 30, "measurement": "m",
            "why_not_threshold_tuning": "w" * 30,
            "mechanism_target": "opponent.rates",
            "expected_diff": "d" * 30, "target_files": ["policy.py"],
            "source_symbols": ["policy.py:f"], "reachable_chain": [],
            "falsifier": {}, "evidence_refs": [], "snapshot_evidence": [],
            "risks": "r" * 30,
        }

    pkt = {
        "schema_version": 2, "valid": True,
        "evidence_mode": "frozen_strength_snapshot",
        "proposal_count": 3,
        "allowed_proposal_ids": ["p1", "p2", "p3"],
        "ordered_proposals": [row("p1"), row("p2"), row("p3")],
    }
    _result, errors = packet._parse_valid_proposal_packet_impl(
        _json.dumps(pkt)
    )
    assert "proposal_packet_change_symbols_not_distinct" in errors

    pkt["ordered_proposals"][1]["change_symbol"] = "policy.py:_other"
    pkt["ordered_proposals"][2]["change_symbol"] = "policy.py:_third"
    _result2, errors2 = packet._parse_valid_proposal_packet_impl(
        _json.dumps(pkt)
    )
    assert "proposal_packet_change_symbols_not_distinct" not in errors2




# --- 1.4 precommit shape + 1.5 repair reproduction context -------------------


def test_precommit_default_n_games_activates_parent_gate():
    """12 matches/opponent >= PRECOMMIT_PARENT_MIN_SAMPLES(6), so the
    'must not lose to parent' gate can actually fire (at the old default 4
    it was structurally unreachable)."""
    import tool_eval
    from strength_order import PRECOMMIT_PARENT_MIN_SAMPLES

    assert tool_eval.PRECOMMIT_DEFAULT_N_GAMES >= PRECOMMIT_PARENT_MIN_SAMPLES
    assert tool_eval.PRECOMMIT_MIN_N_GAMES <= 12 <= tool_eval.PRECOMMIT_MAX_N_GAMES


def test_budget_scaling_evidence_carries_reproduction_context():
    import runtime_architecture_policy as rap

    evidence = rap._budget_scaling_evidence({
        "capability_issues": ["refinement_never_changes_sanitized_decision"],
        "changes_sanitized_decision": False,
        "short": {"trusted_steps": 2},
        "long": {"trusted_steps": 9},
    })
    scenario = evidence["scenario"]
    assert "river_facing_large_bet" in scenario
    assert "20260710" in scenario
    assert "1.7" in scenario and "7.4" in scenario
    assert "sanitized decision changed" in scenario


def test_publication_reconciliation_tolerates_abandoned_gap():
    """v188 (2026-08-16): after v187 was abandoned, the checkpoint's
    epoch_binding records pre-publish high-water 186 while target is 188 —
    the old `== target - 1` clause made the self-publication exemption
    structurally unreachable for every post-abandon publication."""
    import checkpoint_schema as cs

    def _ckpt(binding_hw):
        return {
            "checkpoint_schema_version": cs.CHECKPOINT_SCHEMA_VERSION
            if hasattr(cs, "CHECKPOINT_SCHEMA_VERSION") else 2,
            "next_v": 188,
            "stage": "publishing",
            "epoch_binding": {
                "published_high_water": binding_hw,
                "abandoned_receipt_floor": 186,
                "abandoned_receipt_head_digest": None,
            },
        }

    kwargs = dict(
        published_high_water=188,
        abandoned_receipt_floor=186,
        abandoned_receipt_head_digest=None,
        allow_published_target_reconciliation=True,
    )
    errors_gap = cs.live_checkpoint_allocation_authority_errors(
        _ckpt(186), **kwargs
    )
    assert errors_gap == []
    # A binding at or above target still cannot reconcile.
    errors_bad = cs.live_checkpoint_allocation_authority_errors(
        _ckpt(188), **kwargs
    )
    assert errors_bad


def test_saturator_quota_backoff_pauses_launches():
    import time as _time

    import llm_saturator

    before = llm_saturator._quota_pause_until
    llm_saturator._note_saturator_provider_failure(
        "SATURATOR: LLM unavailable [quota_429]: provider quota window exhausted"
    )
    try:
        assert llm_saturator._saturator_provider_paused() is True
        assert llm_saturator._quota_pause_until > _time.time()
    finally:
        llm_saturator._quota_pause_until = before
    # Benign errors do not pause.
    llm_saturator._note_saturator_provider_failure("timeout reading stream")
    assert llm_saturator._saturator_provider_paused() is False


def test_evidence_tiers_anneal_during_cold_start(tmp_path):
    """2026-08-17: the rating identity reset archives ALL H2H/bot_stats
    payloads, so no row reaches 30/200 right after a reset — v189-v194
    burned five generations at master. The tiers must anneal to the best
    available evidence (shared floor 15), harden automatically, and treat an
    UNREADABLE pool as unknown (absolute tiers), never as empty."""
    import agent_master_validation as amv

    snap = tmp_path / "rich"
    snap.mkdir()
    (snap / "head_to_head.json").write_text(
        json.dumps({"a vs b": {"games": 250, "a_wins": 120, "b_wins": 130}}),
        encoding="utf-8",
    )
    assert amv._effective_evidence_tiers(snap) == (30, 200)

    cold = tmp_path / "cold"
    cold.mkdir()
    (cold / "head_to_head.json").write_text(
        json.dumps({"a vs b": {"games": 31, "a_wins": 15, "b_wins": 16}}),
        encoding="utf-8",
    )
    assert amv._effective_evidence_tiers(cold) == (30, 31)
    # A 31-game row passes during cold start; a 26-game row still fails.
    assert amv._snapshot_evidence_two_tier_errors([31, 31], cold) == []
    assert amv._snapshot_evidence_two_tier_errors([26], cold)

    frozen = tmp_path / "frozen"
    frozen.mkdir()
    (frozen / "head_to_head.json").write_text(
        json.dumps({"a vs b": {"games": 12, "a_wins": 6, "b_wins": 6}}),
        encoding="utf-8",
    )
    # Below the shared 15-game floor nothing passes: citing 12-game rows as
    # load-bearing IS noise fitting — the generation waits for the pool.
    assert amv._effective_evidence_tiers(frozen) == (15, 15)

    # Unknown pool -> absolute tiers (no silent weakening on read failure).
    assert amv._effective_evidence_tiers(None) == (30, 200)


def test_audit_floor_anneals_for_cold_start(monkeypatch):
    import evidence_snapshot as es

    monkeypatch.setattr(
        es, "load_generation_evaluation_snapshot",
        lambda next_v: {
            "available": True,
            "h2h": {"a vs b": {"games": 31, "a_wins": 15, "b_wins": 16}},
            "bot_stats": {"a": {"games": 31, "wins": 15}},
        },
    )
    errs = es.statistical_evidence_floor_errors(
        {
            "worker_prompt": "a vs b games=31 weakness",
            "evidence_refs": ["snapshot:bot_stats.json#/a"],
        },
        195,
    )
    assert errs == []
