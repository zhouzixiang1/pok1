"""P1 deep-sampling priority lane for daemon match selection (2026-10-06).

Delivery blocker being fixed: the master statistical-evidence gate rejects
plans with ``cited_sample_too_small`` (68 rejections / 8h, top blocker) while
the rating pool spreads games breadth-first — 0 of 65 H2H pairs reach the
30-game tier and 60 sit below the annealed 15-game floor.  Master needs
*specific-pair* depth (the current generation's ``source_v`` / crossover
``parent2_v`` matchups); the daemon's breadth objective cannot express that.

Contract under test (selection logic only — no governor/env changes, no
``n_pairs`` change):

1. Focus resolution reads ``RESULTS_DIR/pipeline_state.json`` at every
   scheduling pass: ``active_generation.source_v`` first, top-level
   ``source_v`` as the on-disk shape, crossover ``parent2_v`` included when
   valid.  Unreadable/invalid/absent source falls back to the newest
   published pool bot.
2. Priority set = every pool pair involving a focus bot, in BOTH wire
   directions; an ordered direction stays in the lane while it has < 30
   admitted 70-hand samples.  All focus pairs saturated (both directions
   >= 30) -> the lane empties and selection is byte-equivalent to the legacy
   breadth objective.
3. The legacy breadth objective is never starved: per round, at least
   ``max(1, n_picks // 4)`` picks are reserved for pairs with NO unsaturated
   focus membership, and those pairs are ordered before focus pairs in the
   breadth fill.
4. Observability: explicit lane events (enabled / saturated / source
   fallback) on phase transition only, plus in-memory stats counters merged
   into the daemon stats file at cycle save.  No new must-persist state:
   directional counts are derived from the existing authoritative
   ``match_history.jsonl`` (append-only) and rebuilt after any crash.

Identity hard constraint (proven in the same file, production-sourced):
``runtime_profile`` bytes — the exact dict ``_rating_protocol_config``
freezes into ``evaluation_data_identity`` — are byte-identical before and
after the lane change.  NOTE the honest boundary: ``web/core/elo_daemon.py``
is a ``SEMANTIC_PATHS`` member, so the *evaluator* identity hash of that
file changes with any selection edit and deploy requires the documented
operator archive-and-restart decision (``scripts/check_rating_identity_predeploy.py``,
same flow as the 2026-10-05 governor deploy).  The rating-identity *runtime
profile* — the part that pins ``n_pairs`` — is what these tests prove
byte-identical, from the production builder, against a golden capture taken
from the pre-change code (verified equal to the live production manifest's
``runtime_profile`` digest at capture time).
"""

import json
import sys
from pathlib import Path

import pytest

WEB_CORE = Path(__file__).resolve().parents[1] / "core"
if str(WEB_CORE) not in sys.path:
    sys.path.insert(0, str(WEB_CORE))

import elo_daemon  # noqa: E402
import elo_daemon_deep_sampling as edds  # noqa: E402

from bot_namespace import bot_name  # noqa: E402
from evaluation_data_identity import (  # noqa: E402
    canonical_digest,
    ensure_evaluation_data_identity,
)

POOL = [bot_name(v) for v in (1, 11, 79, 188)]
N1, N11, N79, N188 = POOL


# ─── fixtures ────────────────────────────────────────────────────────────────


def _write_match_history(path, rows):
    """rows: iterable of (bot0_name, bot1_name, count)."""
    lines = []
    for a, b, count in rows:
        for _ in range(count):
            lines.append(json.dumps({"bot0": a, "bot1": b}))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


@pytest.fixture
def lane_env(tmp_path, monkeypatch):
    """Point the daemon's scheduling inputs at an isolated results dir."""
    results = tmp_path / "results"
    results.mkdir()
    _patch_daemon_paths(monkeypatch, results)
    return results


def _patch_daemon_paths(monkeypatch, results):
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", results)
    monkeypatch.setattr(elo_daemon, "MATCH_HISTORY_FILE", results / "match_history.jsonl")
    monkeypatch.setattr(elo_daemon, "PRIORITY_EVAL_FILE", results / "priority_eval.json")


def _pipeline_state(results, *, source_v=None, parent2_v=None, raw=None):
    if raw is not None:
        (results / "pipeline_state.json").write_text(raw, encoding="utf-8")
        return
    state = {}
    if source_v is not None:
        state["source_v"] = source_v
    if parent2_v is not None:
        state["parent2_v"] = parent2_v
    (results / "pipeline_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )


def _pick(n_picks=6, active_bots=None):
    return elo_daemon.pick_matches(
        active_bots or POOL, {}, {}, n_picks=n_picks
    )


def _is_reversed(pair):
    """True when the tuple orders the LATER pool bot first (a lane-only shape:
    the legacy selection only ever emits pool-order tuples)."""
    ia = POOL.index(pair[0])
    ib = POOL.index(pair[1])
    return ia > ib


def _focus_members(pair, focus):
    return [x for x in pair if x in focus]


# ─── 1. lane prioritizes source-parent pairs, both directions ────────────────


def test_lane_prioritizes_source_and_parent2_pairs_both_directions(lane_env):
    _pipeline_state(lane_env, source_v=11, parent2_v=188)
    _write_match_history(
        lane_env / "match_history.jsonl", [(N11, N188, 21), (N1, N11, 5)]
    )

    picked = _pick(n_picks=6)
    focus = {N11, N188}

    # Lane picks dominate: at least n_picks - floor picks involve a focus bot.
    focus_picks = [p for p in picked if _focus_members(p, focus)]
    assert len(focus_picks) >= 5, picked

    # The never-played directions of the source parent are scheduled, and the
    # reverse wire direction (later pool bot first) is emitted — the legacy
    # selection could never produce that ordering.
    assert any(_is_reversed(p) for p in focus_picks), picked
    assert any(set(p) == {N1, N11} for p in picked), picked

    # Every pick is a real pool pair in both-bot-distinct form.
    for a, b in picked:
        assert a in POOL and b in POOL and a != b


def test_lane_counts_both_wire_directions_independently(lane_env):
    """A pair whose forward direction is at target but reverse is not stays
    in the lane, scheduling only the reverse direction."""
    _pipeline_state(lane_env, source_v=11)
    rows = [
        (N1, N11, 30),   # forward at target
        (N11, N1, 0),    # reverse never sampled
        (N11, N79, 30),
        (N79, N11, 30),
        (N11, N188, 30),
        (N188, N11, 30),
    ]
    _write_match_history(lane_env / "match_history.jsonl", rows)

    picked = _pick(n_picks=4)
    lane = [p for p in picked if N11 in p]
    # The unsaturated direction (N11 at seat-0) is offered ...
    assert (N11, N1) in lane, picked
    # ... and the saturated directions are not re-emitted by the lane.
    assert (N1, N11) not in lane, picked


def test_lane_without_parent2_does_not_focus_the_other_parent(lane_env):
    _pipeline_state(lane_env, source_v=11)
    _write_match_history(lane_env / "match_history.jsonl", [])

    picked = _pick(n_picks=6)
    # Every lane pick must involve v11; pairs among non-focus bots only.
    for a, b in picked:
        if _is_reversed((a, b)) or N11 in (a, b):
            assert N11 in (a, b), picked


# ─── 2. saturation returns to the legacy breadth objective ──────────────────


def test_lane_saturated_when_both_directions_reach_target(lane_env):
    _pipeline_state(lane_env, source_v=11, parent2_v=188)
    rows = []
    for other in (N1, N79):
        rows.append((N11, other, 30))
        rows.append((other, N11, 30))
        rows.append((N188, other, 30))
        rows.append((other, N188, 30))
    rows.append((N11, N188, 30))
    rows.append((N188, N11, 30))
    _write_match_history(lane_env / "match_history.jsonl", rows)

    picked = _pick(n_picks=6)

    # Lane is empty: every emitted tuple is in pool order (legacy shape) and
    # selection is exactly the legacy breadth candidate space.
    assert all(not _is_reversed(p) for p in picked), picked
    lane = edds.lane_selection(
        active_bots=POOL,
        n_picks=6,
        base_max=3,
        coverage={b: 0.0 for b in POOL},
        priority_fn=lambda a, b: 0.0,
        priority_bot=None,
    )
    assert lane.pairs == ()
    assert lane.phase == "saturated"


def test_lane_full_saturation_equals_legacy_selection_shape(lane_env):
    """With the lane saturated the selected set is drawn only from the legacy
    i<j candidate space and respects the legacy per-bot caps."""
    _pipeline_state(lane_env, source_v=188)
    rows = [
        (N188, N1, 30), (N1, N188, 30),
        (N188, N11, 30), (N11, N188, 30),
        (N188, N79, 30), (N79, N188, 30),
    ]
    _write_match_history(lane_env / "match_history.jsonl", rows)

    picked = _pick(n_picks=6)
    legacy_space = {
        (a, b)
        for i, a in enumerate(POOL)
        for b in POOL[i + 1:]
    }
    assert picked, "selection must not empty the queue"
    assert all(p in legacy_space for p in picked), picked


# ─── 3. fallbacks ────────────────────────────────────────────────────────────


def test_lane_falls_back_to_newest_published_when_state_unreadable(lane_env):
    # No pipeline_state.json at all.
    picked = _pick(n_picks=6)
    focus = {N188}
    focus_picks = [p for p in picked if _focus_members(p, focus)]
    assert len(focus_picks) >= 5, picked
    assert any(_is_reversed(p) for p in focus_picks), picked


def test_lane_falls_back_on_broken_state_json(lane_env):
    _pipeline_state(lane_env, raw="{not json")
    _write_match_history(lane_env / "match_history.jsonl", [])

    picked = _pick(n_picks=6)
    focus_picks = [p for p in picked if N188 in p]
    assert len(focus_picks) >= 5, picked


def test_lane_falls_back_when_source_v_not_in_pool(lane_env):
    _pipeline_state(lane_env, source_v=999)
    picked = _pick(n_picks=6)
    focus_picks = [p for p in picked if N188 in p]
    assert len(focus_picks) >= 5, picked


def test_lane_reads_active_generation_nested_source(lane_env):
    state = {"active_generation": {"source_v": 79, "parent2_v": 11}}
    (lane_env / "pipeline_state.json").write_text(json.dumps(state), encoding="utf-8")
    picked = _pick(n_picks=6)
    focus = {N11, N79}
    focus_picks = [p for p in picked if _focus_members(p, focus)]
    assert len(focus_picks) >= 5, picked


# ─── 4. breadth is never starved ────────────────────────────────────────────


def test_breadth_floor_keeps_non_focus_pairs_scheduled(lane_env):
    _pipeline_state(lane_env, source_v=11, parent2_v=188)
    _write_match_history(lane_env / "match_history.jsonl", [])

    picked = _pick(n_picks=6)
    focus = {N11, N188}
    breadth = [p for p in picked if not _focus_members(p, focus)]
    assert breadth, picked  # at least max(1, 6//4) == 1 non-focus pick


def test_breadth_floor_scales_with_pick_budget(lane_env):
    _pipeline_state(lane_env, source_v=11)
    _write_match_history(lane_env / "match_history.jsonl", [])

    picked = _pick(n_picks=8)
    focus = {N11}
    breadth = [p for p in picked if not _focus_members(p, focus)]
    assert len(breadth) >= 2, picked  # max(1, 8//4) == 2


# ─── 5. observability: events + stats, no new persisted state ───────────────


def test_lane_events_fire_on_transitions(lane_env, monkeypatch):
    seen = []
    monkeypatch.setattr(edds, "_emit_lane_event", lambda *a, **k: seen.append(a))

    _pipeline_state(lane_env, source_v=11, parent2_v=188)
    _pick(n_picks=6)
    enabled = [e for e in seen if e[0] == "daemon.deep_sampling_lane_enabled"]
    assert enabled, seen

    # Saturate every focus pair direction; the next selection must emit the
    # saturated transition.
    rows = []
    for focus_bot in (N11, N188):
        for other in POOL:
            if other == focus_bot:
                continue
            rows.append((focus_bot, other, 30))
            rows.append((other, focus_bot, 30))
    _write_match_history(lane_env / "match_history.jsonl", rows)
    _pick(n_picks=6)
    saturated = [e for e in seen if e[0] == "daemon.deep_sampling_lane_saturated"]
    assert saturated, seen

    # Transition-only: repeated selections in the same phase do not re-emit.
    before = len(seen)
    _pick(n_picks=6)
    assert len(seen) == before, seen


def test_lane_fallback_event_on_unreadable_state(lane_env, monkeypatch):
    seen = []
    monkeypatch.setattr(edds, "_emit_lane_event", lambda *a, **k: seen.append(a))
    _pick(n_picks=6)
    fallback = [e for e in seen if e[0] == "daemon.deep_sampling_lane_source_fallback"]
    assert fallback, seen
    enabled = [e for e in seen if e[0] == "daemon.deep_sampling_lane_enabled"]
    assert enabled, seen


def test_lane_disabled_phase_is_silent(lane_env, monkeypatch):
    """No lane possible (tiny pool / unresolvable focus) -> no events at all.
    Regression: ``disabled`` used to fall into the enabled-event branch and
    emitted ``..._enabled`` with an empty focus list (observed in the operator
    event ledger during bring-up)."""
    seen = []
    monkeypatch.setattr(edds, "_emit_lane_event", lambda *a, **k: seen.append(a))
    edds.reset_lane_state_for_tests()

    sel = edds.lane_selection(
        active_bots=[N1], n_picks=6, base_max=3,
        coverage={N1: 0.0}, priority_fn=lambda a, b: 0.0, priority_bot=None,
    )
    assert sel.phase == "disabled"
    assert sel.pairs == ()

    sel2 = edds.lane_selection(
        active_bots=["alpha", "beta"], n_picks=6, base_max=3,
        coverage={"alpha": 0.0, "beta": 0.0},
        priority_fn=lambda a, b: 0.0, priority_bot=None,
    )
    assert sel2.phase == "disabled"
    assert seen == []


def test_lane_stats_counters_accumulate(lane_env):
    edds.reset_lane_state_for_tests()
    _pipeline_state(lane_env, source_v=11, parent2_v=188)
    _write_match_history(lane_env / "match_history.jsonl", [])
    first = edds.lane_stats_snapshot()
    assert first.get("selections", 0) == 0
    _pick(n_picks=6)
    second = edds.lane_stats_snapshot()
    assert second["selections"] == 1
    assert second.get("lane_picks", 0) > 0
    _pick(n_picks=6)
    third = edds.lane_stats_snapshot()
    assert third["selections"] == 2
    assert third["lane_picks"] >= second["lane_picks"]


def test_lane_cycle_stats_merge_writes_counters(lane_env):
    from elo_daemon_persistence import _merge_deep_sampling_lane_stats

    edds.reset_lane_state_for_tests()
    stats = {"pairs": {}, "total_games": 0}
    _pipeline_state(lane_env, source_v=11)
    _pick(n_picks=6)
    merged = _merge_deep_sampling_lane_stats(dict(stats))
    assert merged["deep_sampling_lane"]["selections"] >= 1
    # The legacy keys are untouched by the merge.
    assert merged["pairs"] == {}
    assert merged["total_games"] == 0


def test_no_new_must_persist_state(lane_env):
    """The lane derives everything from existing inputs: running selections
    must not create new files in the results dir, and the in-memory caches
    rebuild from the same inputs after a simulated crash (state reset)."""
    import random

    _pipeline_state(lane_env, source_v=11, parent2_v=188)
    _write_match_history(
        lane_env / "match_history.jsonl", [(N11, N188, 21), (N1, N11, 5)]
    )
    # Compare authority files only: read_locked_json's shared-lock sidecars
    # (``*.lock``) are pre-existing read infrastructure, not lane state.
    def _authority_names():
        return sorted(
            p.name for p in lane_env.iterdir() if not p.name.endswith(".lock")
        )

    before = _authority_names()
    _pick(n_picks=6)
    _pick(n_picks=6)
    after = _authority_names()
    assert before == after

    random.seed(20261006)
    picked_first = _pick(n_picks=6)
    # simulate a crash: wipe module-local caches (nothing durable to recover)
    edds.reset_lane_state_for_tests()
    random.seed(20261006)
    picked_after = _pick(n_picks=6)
    assert sorted(picked_first) == sorted(picked_after)


# ─── 6. identity hard constraint (production-sourced) ───────────────────────
#
# Golden bytes captured from the PRE-change production builder
# (``elo_daemon._rating_protocol_config(n_pairs=1)`` serialized with
# sort_keys/indent=2) on 2026-10-06; the capture's canonical_digest was
# verified equal to the LIVE production manifest's ``runtime_profile`` digest
# (evaluation_data_manifest.json in the runtime checkout) at capture time.

GOLDEN_RUNTIME_PROFILE_JSON = """{
  "artifact_execution_mode": "direct_content_bound_policy_artifact",
  "national_execution_mode": "native_tcp",
  "national_hands": 70,
  "national_matches": 1,
  "native_match_timing_plan": {
    "artifact_preparation_per_bot_timeout_us": 30000000,
    "artifact_preparation_timeout_us": 60000000,
    "betting_rounds_per_hand": 4,
    "bot_a": {
      "action_delay_us": 0,
      "baseline_target_us": 200000,
      "hard_deadline_us": 2000000,
      "refinement_budget_us": 1800000
    },
    "bot_b": {
      "action_delay_us": 0,
      "baseline_target_us": 200000,
      "hard_deadline_us": 2000000,
      "refinement_budget_us": 1800000
    },
    "capacity_queue_timeout_us": 300000000,
    "cleanup_timeout_us": 35000000,
    "connect_timeout_us": 20000000,
    "decision_slot_us": 2250000,
    "effective_timeout_us": 5415000000,
    "engine_action_cap_per_betting_round": 100,
    "execution_timeout_us": 5600000000,
    "finalization_timeout_us": 65000000,
    "first_strict_lease_timeout_us": 5960000000,
    "fixed_liveness_slack_us": 60000000,
    "hands": 70,
    "launch_timeout_us": 480000000,
    "liveness_floor_us": 5415000000,
    "name_timeout_us": 30000000,
    "national_hand_action_request_cap": 34,
    "post_execution_completion_timeout_us": 30000000,
    "process_drain_timeout_us": 5000000,
    "profile_definition_digest": "9594029979c053f6d4815e6fdfb6e2282b76e1eab25d32555422dc9f442c6686",
    "profile_id": "national_local_strength_v1",
    "protocol_action_timeout_us": 60000000,
    "requested_timeout_us": 280000000,
    "schema_version": 5,
    "startup_timeout_us": 120000000,
    "timing_plan_digest": "0959cadb2dea10e723cf74cfe75880d8808c25e27f3d4b27f4671c53121caa7c"
  },
  "native_match_timing_plan_digest": "0959cadb2dea10e723cf74cfe75880d8808c25e27f3d4b27f4671c53121caa7c",
  "profile_id": "national_native",
  "protocol": "national"
}"""

GOLDEN_RUNTIME_PROFILE_DIGEST = (
    "1a453404cb0bf202c35101f1e1664b1a68123e676972c0033dba00bb3395fd35"
)

GOLDEN_RUNTIME_PROFILE_KEYS = (
    "artifact_execution_mode",
    "national_execution_mode",
    "national_hands",
    "national_matches",
    "native_match_timing_plan",
    "native_match_timing_plan_digest",
    "profile_id",
    "protocol",
)


@pytest.fixture
def identity_env(tmp_path, monkeypatch):
    for var in (
        "POK_WORKFLOW_PROFILE",
        "POK_NATIONAL_RATING_MATCHES",
        "POK_RATING_PROTOCOL",
    ):
        monkeypatch.delenv(var, raising=False)
    # Isolate the lane's file inputs too: the profile builder must stay
    # byte-stable no matter what lane state a real selection warmed up.
    results = tmp_path / "lane-results"
    results.mkdir()
    _patch_daemon_paths(monkeypatch, results)
    (results / "pipeline_state.json").write_text(
        json.dumps({"source_v": 11, "parent2_v": 188}), encoding="utf-8"
    )


def _profile_with_lane_active():
    """Warm the deep-sampling lane (real selection over fixture inputs) and
    then build the runtime profile through the production path."""
    edds.reset_lane_state_for_tests()
    elo_daemon.pick_matches(POOL, {}, {}, n_picks=6)
    return elo_daemon._rating_protocol_config(n_pairs=1)


def test_runtime_profile_bytes_unchanged_by_lane(identity_env):
    cfg = _profile_with_lane_active()
    serialized = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True)
    assert serialized == GOLDEN_RUNTIME_PROFILE_JSON
    assert tuple(sorted(cfg)) == GOLDEN_RUNTIME_PROFILE_KEYS


def test_runtime_profile_canonical_digest_unchanged_by_lane(identity_env):
    cfg = _profile_with_lane_active()
    assert canonical_digest(cfg) == GOLDEN_RUNTIME_PROFILE_DIGEST
    # Lane explicitly disabled -> same digest (selection state cannot leak).
    edds.reset_lane_state_for_tests()
    cfg_off = elo_daemon._rating_protocol_config(n_pairs=1)
    assert canonical_digest(cfg_off) == GOLDEN_RUNTIME_PROFILE_DIGEST


def test_production_manifest_accepts_post_change_profile(identity_env, tmp_path):
    """Same-source manifest proof: initialize a fresh manifest with the
    PRE-change golden profile, then re-ensure with the profile built by the
    POST-change code.  Byte equality is proven by the production comparison
    itself — a single differing byte raises
    ``rating daemon runtime profile changed; archive and restart ratings``."""
    golden = json.loads(GOLDEN_RUNTIME_PROFILE_JSON)
    results = tmp_path / "results"
    results.mkdir()
    first = ensure_evaluation_data_identity(results, runtime_profile=golden)
    manifest_file = results / "evaluation_data_manifest.json"
    bytes_after_first = manifest_file.read_bytes()

    cfg = _profile_with_lane_active()
    second = ensure_evaluation_data_identity(results, runtime_profile=cfg)
    assert second["manifest_digest"] == first["manifest_digest"]
    assert manifest_file.read_bytes() == bytes_after_first  # no rewrite happened


def test_pairs_knob_remains_the_identity_boundary(identity_env):
    """Documentation test: ``n_pairs`` IS pinned inside runtime_profile —
    changing it changes the digest (why the lane must never touch it)."""
    pinned = canonical_digest(elo_daemon._rating_protocol_config(n_pairs=1))
    other = canonical_digest(elo_daemon._rating_protocol_config(n_pairs=2))
    assert pinned != other
