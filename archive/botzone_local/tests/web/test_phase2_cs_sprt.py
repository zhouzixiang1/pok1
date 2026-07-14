"""Phase 2 tests: Confidence Sequences, AgentAssay SPRT, behavior fingerprints.

Pure-function + synthetic-mock tests — no real subprocess battles are spawned.
Covers:
  A. anytime-valid Confidence Sequence (eval_stats.confidence_sequence_ci,
     sequential_decision): half-width formula, empty handling, anytime
     validity coverage, three-state decision logic.
  B. AgentAssay SPRT (decision_tester.run_decision_tests_sprt): A/B boundary
     numerics, fast H0/H1 acceptance, n_max fallback, return shape.
  C. Behavior fingerprints (replay_analysis.extract_behavior_fingerprint,
     fingerprint_distance): zero self-distance, high distance for divergent
     bots, normalized per-street freq, empty-games safety, string-summary
     regression.
  D. Generator early-stop (tool_eval._run_single_mirror_battle via the MCP
     handler): reject early-stop on parent, flag-OFF fallback parity, W/L
     reconstruction from the generator stream, early-stop ≠ incomplete,
     aggregate gate unchanged.
  E. Feature-flag default + existing-bootstrap regression.
"""

import asyncio
import json
import math
import random
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "web" / "core"))
sys.path.insert(0, str(_PROJECT_ROOT / "web" / "server"))

from eval_stats import (
    confidence_sequence_ci,
    sequential_decision,
    paired_bootstrap_ci,
    NET_CHIPS_RANGE,
)
import decision_tester as dt
import replay_analysis as ra
import tool_eval
from tool_eval import run_precommit_eval as _run_precommit_eval_tool

run_precommit_eval = _run_precommit_eval_tool.handler

# Ensure the engine.battle module is loaded so sys.modules["engine.battle"] can
# be patched. tool_eval imports it lazily inside run_precommit_eval, and the
# `engine` package __init__ re-exports a `battle` *function* that shadows the
# submodule name, so `import engine.battle` resolves to the function. Use
# sys.modules to grab the real module object.
import engine  # noqa: F401  (registers the engine package)
try:
    import importlib
    _battle_module_init = importlib.import_module("engine.battle")
except Exception:
    _battle_module_init = None


# ──────────────────────────────────────────────
# A. Confidence Sequence
# ──────────────────────────────────────────────


class TestConfidenceSequence:
    def test_cs_ci_halfwidth_formula(self):
        """Exact half-width for t=8, α=0.05, R=NET_CHIPS_RANGE."""
        samples = [1000.0] * 8
        lo, hi, hw = confidence_sequence_ci(samples, alpha=0.05, R=NET_CHIPS_RANGE)
        t = 8
        expected_hw = NET_CHIPS_RANGE * math.sqrt(
            math.log((math.pi ** 2) * (t ** 2) / (6.0 * 0.05)) / (2.0 * t)
        )
        assert hw == pytest.approx(expected_hw, rel=1e-12)
        mean = 1000.0
        assert lo == pytest.approx(mean - expected_hw)
        assert hi == pytest.approx(mean + expected_hw)

    def test_cs_ci_empty_returns_none(self):
        lo, hi, hw = confidence_sequence_ci([])
        assert (lo, hi, hw) == (None, None, None)

    def test_cs_halfwidth_shrinks_with_t(self):
        """Half-width must monotonically decrease as more samples arrive."""
        hw_prev = None
        for t in range(1, 60):
            _, _, hw = confidence_sequence_ci([0.0] * t, alpha=0.05, R=NET_CHIPS_RANGE)
            if hw_prev is not None:
                assert hw < hw_prev
            hw_prev = hw

    def test_cs_anytime_validity_coverage(self):
        """Anytime-valid: peeking at every t, CI covers the true mean ≥ 1-α.

        With a true mean μ=0 and bounded uniform noise in [-2000, 2000]
        (R=80000 dominates so the sub-Gaussian bound holds), run 1500 trials.
        For each trial pick a random stopping time t∈[1,25] and check whether
        the (1-α) anytime-valid CI at that t contains 0. The coverage must be
        ≥ 1-α (0.95). A fixed-N bootstrap peeked repeatedly would break this.
        """
        rng = random.Random(20260616)
        alpha = 0.05
        trials = 1500
        covered = 0
        for _ in range(trials):
            t = rng.randint(1, 25)
            xs = [rng.uniform(-2000, 2000) for _ in range(t)]
            lo, hi, _ = confidence_sequence_ci(xs, alpha=alpha, R=NET_CHIPS_RANGE)
            if lo is not None and lo <= 0.0 <= hi:
                covered += 1
        coverage = covered / trials
        # Anytime-valid guarantee: coverage ≥ 1-α (with small Monte-Carlo slack).
        assert coverage >= 1 - alpha - 0.02, f"coverage {coverage:.3f} < {1 - alpha - 0.02}"

    def test_cs_anytime_type_i_synthetic(self):
        """Synthetic no-difference stream: premature-stop false-reject rate ≤ α·3.

        True mean exactly 0; reject_threshold=-2000. Across 1000 independent
        streams of length 16, the fraction that hit DECIDE_REJECT at ANY t must
        stay well below α (sub-Gaussian CS keeps the miscoverage ≤ α across all
        stopping times). Allow α×3 tolerance for the Monte-Carlo noise + the
        conservative fixed R.
        """
        rng = random.Random(7)
        alpha = 0.05
        reject_threshold = -2000.0
        n_streams = 1000
        rejections = 0
        for _ in range(n_streams):
            xs = []
            hit = False
            for _ in range(16):
                xs.append(rng.uniform(-2000, 2000))
                d = sequential_decision(
                    xs,
                    reject_threshold=reject_threshold,
                    accept_threshold=None,
                    alpha=alpha,
                    R=NET_CHIPS_RANGE,
                )
                if d["decision"] == "DECIDE_REJECT":
                    hit = True
                    break
            if hit:
                rejections += 1
        rate = rejections / n_streams
        assert rate <= alpha * 3, f"false-reject rate {rate:.3f} > α·3={alpha*3}"

    def test_cs_sequential_decision_reject(self):
        """A strongly negative stream eventually triggers DECIDE_REJECT.

        With fixed R=80000 and threshold -2000, a mean of -30000 over enough
        samples must cross the CI upper-bound test (ci_hi < -2000).
        """
        # -30000 mean is within the bounded range; need enough t for hw < 28000.
        for t in (1, 5, 10, 15, 20, 30, 40, 60, 80):
            d = sequential_decision(
                [-30000.0] * t,
                reject_threshold=-2000.0,
                accept_threshold=None,
                R=NET_CHIPS_RANGE,
            )
            if d["decision"] == "DECIDE_REJECT":
                return
        pytest.fail("DECIDE_REJECT never triggered for strongly negative stream")

    def test_cs_sequential_decision_continue_near_zero(self):
        """Near-zero mean never rejects at small t (CI straddles threshold)."""
        xs = [(-1) ** i * 100 for i in range(16)]  # mean ≈ 0
        d = sequential_decision(
            xs, reject_threshold=-2000.0, accept_threshold=2000.0,
            R=NET_CHIPS_RANGE,
        )
        # With R=80000 the CI is far wider than ±2000, so no decision at t=16.
        assert d["decision"] in ("CONTINUE", "UNDECIDED_AT_LIMIT")

    def test_cs_sequential_decision_accept(self):
        """A strongly positive stream eventually triggers DECIDE_ACCEPT."""
        for t in (1, 5, 10, 20, 40, 80):
            d = sequential_decision(
                [30000.0] * t,
                reject_threshold=None,
                accept_threshold=2000.0,
                R=NET_CHIPS_RANGE,
            )
            if d["decision"] == "DECIDE_ACCEPT":
                return
        pytest.fail("DECIDE_ACCEPT never triggered for strongly positive stream")

    def test_cs_sequential_decision_undecided_at_limit(self):
        """n_max reached with no decision → UNDECIDED_AT_LIMIT."""
        d = sequential_decision(
            [100.0] * 16,
            reject_threshold=-2000.0,
            accept_threshold=2000.0,
            n_max=16,
            R=NET_CHIPS_RANGE,
        )
        assert d["decision"] == "UNDECIDED_AT_LIMIT"
        assert d["n"] == 16
        assert "n_max=16" in d["rule"]

    def test_cs_sequential_decision_shape(self):
        d = sequential_decision([1.0, 2.0, 3.0], reject_threshold=-2000.0)
        for k in ("decision", "ci_lo", "ci_hi", "half_width", "mean", "n", "rule"):
            assert k in d
        assert d["n"] == 3


# ──────────────────────────────────────────────
# B. AgentAssay SPRT
# ──────────────────────────────────────────────


def _sprt_with_bernoulli(monkeypatch, p, seed=123):
    """Monkeypatch run_single_scenario to a Bernoulli(p) draw and run SPRT."""
    rng = random.Random(seed)

    def fake(_bot_path, _scenario):
        return (rng.random() < p, "ok")

    monkeypatch.setattr(dt, "run_single_scenario", fake)
    return dt.run_decision_tests_sprt("fake", {"input": {}}, seed=None)


def _sprt_with_fixed_sequence(monkeypatch, outcomes):
    """Monkeypatch run_single_scenario to replay a fixed pass/fail list.

    `outcomes` is a list of 0/1 consumed in order; the SPRT stops at the first
    boundary crossing or after n_max, whichever is first. Lets tests pin an
    exact LLR trajectory to exercise the n_max fallback deterministically.
    """
    it = iter(outcomes)

    def fake(_bot_path, _scenario):
        try:
            x = next(it)
        except StopIteration:
            x = 1
        return (bool(x), "ok" if x else "fail")

    monkeypatch.setattr(dt, "run_single_scenario", fake)
    return dt.run_decision_tests_sprt("fake", {"input": {}}, seed=None)


class TestSPRT:
    def test_sprt_bounds_correct(self):
        """A=ln(18)=2.890, B=ln(0.1053)=-2.251."""
        hi, lo = dt._sprt_bounds(alpha=0.05, beta=0.10)
        assert hi == pytest.approx(math.log(18.0))          # ln((1-0.1)/0.05)
        assert lo == pytest.approx(math.log(0.1 / 0.95))    # ln(0.1/(1-0.05))

    def test_sprt_llr_signs(self):
        # All passes (H0 evidence) ⇒ negative LLR (toward H0).
        assert dt._sprt_llr([1, 1, 1, 1]) < 0
        # All fails (H1 evidence) ⇒ positive LLR (toward H1).
        assert dt._sprt_llr([0, 0, 0, 0]) > 0

    def test_sprt_accepts_h0_fast(self, monkeypatch):
        """p=0.95 (far above p0=0.85) ⇒ quick PASS via sprt_h0."""
        r = _sprt_with_bernoulli(monkeypatch, p=0.95, seed=1)
        assert r["decision"] == "PASS"
        assert r["final_rule"] == "sprt_h0"
        # ASN for a clearly-good bot should be small.
        assert r["n_trials"] <= 8

    def test_sprt_accepts_h1_fast(self, monkeypatch):
        """p=0.30 (far below p1=0.60) ⇒ quick FAIL via sprt_h1."""
        r = _sprt_with_bernoulli(monkeypatch, p=0.30, seed=2)
        assert r["decision"] == "FAIL"
        assert r["final_rule"] == "sprt_h1"
        assert r["n_trials"] <= 8

    def test_sprt_n_max_fallback_pass(self, monkeypatch):
        """A boundary-avoidant sequence hits n_max then presumptive PASS.

        Sequence: 9 pass + 3 fail interleaved so the cumulative LLR never crosses
        ln(B) or ln(A). Final LLR ≈ -0.19 (between bounds), rate 0.75 ≥ 0.7.
        Truncation without a crossing defaults to PASS (type-I safe).
        """
        # Interleave to keep the running LLR inside (-2.251, 2.890) throughout.
        seq = [1, 1, 1, 0, 1, 0, 1, 1, 0, 1, 1, 1]
        r = _sprt_with_fixed_sequence(monkeypatch, seq)
        assert r["n_trials"] == dt.SPRT_N_MAX
        assert r["final_rule"] == "n_max_default_pass"
        assert r["decision"] == "PASS"
        assert r["pass_rate"] >= 0.7

    def test_sprt_n_max_truncated_default_pass(self, monkeypatch):
        """Truncation without a boundary crossing ⇒ presumptive PASS even when
        rate < 0.7. The SPRT only FAILs on an LLR crossing of ln(A); a rate-based
        FAIL at truncation would inflate type-I to ~2α (P(rate<0.7 | p0) ≈ 0.085
        at n_max=12), so truncation defaults to PASS. Severe regressions (p ≤ p1)
        cross ln(A) well before n_max and still FAIL (see test_sprt_accepts_h1_fast).

        Sequence: 8 pass + 4 fail, running LLR stays inside the bounds
        (max ≈ 1.2 < ln(18)=2.89); rate 0.667 < 0.7.
        """
        seq = [1, 0, 1, 1, 0, 1, 1, 0, 1, 1, 1, 0]
        r = _sprt_with_fixed_sequence(monkeypatch, seq)
        assert r["n_trials"] == dt.SPRT_N_MAX
        assert r["final_rule"] == "n_max_default_pass"
        assert r["decision"] == "PASS"
        assert r["pass_rate"] < 0.7

    def test_sprt_return_shape(self, monkeypatch):
        r = _sprt_with_bernoulli(monkeypatch, p=0.85, seed=5)
        for k in ("decision", "n_trials", "pass_rate", "passes", "llr",
                  "bound_hi", "bound_lo", "final_rule", "details"):
            assert k in r
        assert r["bound_hi"] == pytest.approx(math.log(18.0))
        assert r["bound_lo"] == pytest.approx(math.log(0.1 / 0.95))


# ──────────────────────────────────────────────
# B2. AgentAssay SPRT aggregate (run_decision_tests_sprt_aggregate)
#     — the gate-ready wrapper that the quality gate consumes when
#     DECISION_TEST_SPRT_ENABLED is True.
# ──────────────────────────────────────────────

def _write_scenarios_file(monkeypatch, tmp_path, scenarios):
    """Point decision_tester at a tmp scenarios file with the given list."""
    f = tmp_path / "test_scenarios.json"
    f.write_text(json.dumps(scenarios), encoding="utf-8")
    # Also neutralize the dynamic-scenarios merge so it does not pull in real data.
    monkeypatch.setattr(dt, "SCENARIOS_FILE", f)
    monkeypatch.setattr(dt, "load_dynamic_scenarios", lambda: [])
    return f


class TestSPRTAggregate:
    def test_all_pass_aggregates_to_pass(self, monkeypatch, tmp_path):
        """Every scenario's SPRT accepts H0 → aggregate pass_rate 1.0, no
        critical_failures, sprt_decisions populated per scenario."""
        _write_scenarios_file(monkeypatch, tmp_path, [
            {"id": "s1", "input": {}, "severity": "critical"},
            {"id": "s2", "input": {}, "severity": "advisory"},
        ])
        rng = random.Random(7)
        monkeypatch.setattr(dt, "run_single_scenario",
                            lambda _b, _s: (rng.random() < 0.95, "ok"))

        r = dt.run_decision_tests_sprt_aggregate("fake")

        # Same dict shape as run_decision_tests_detail.
        for k in ("pass_rate", "passed", "total", "critical_passed",
                  "critical_total", "critical_failures", "failures", "scenarios"):
            assert k in r, f"missing key {k}"
        assert r["total"] == 2
        assert r["passed"] == 2
        assert r["pass_rate"] == 1.0
        assert r["critical_passed"] == 1 and r["critical_total"] == 1
        assert r["critical_failures"] == []
        # Plus the SPRT-specific telemetry list.
        assert len(r["sprt_decisions"]) == 2
        assert all(d["decision"] == "PASS" for d in r["sprt_decisions"])

    def test_critical_fail_blocks_aggregate(self, monkeypatch, tmp_path):
        """A critical scenario whose SPRT accepts H1 → critical_failures non-empty,
        aggregate pass_rate < 1.0, recorded in failures."""
        _write_scenarios_file(monkeypatch, tmp_path, [
            {"id": "preflop_aa_first_act", "input": {}, "severity": "critical"},
            {"id": "s2", "input": {}, "severity": "advisory"},
        ])
        # Critical always fails (p=0.10 < p1=0.60 → sprt_h1 FAIL fast);
        # advisory always passes.
        calls = {"n": 0}

        def fake(_b, _s):
            calls["n"] += 1
            # First scenario (critical) fails, rest pass.
            ok = calls["n"] > 3
            return (ok, "ok" if ok else "fail")

        monkeypatch.setattr(dt, "run_single_scenario", fake)

        r = dt.run_decision_tests_sprt_aggregate("fake")

        crit = [c for c in r["critical_failures"] if c["id"] == "preflop_aa_first_act"]
        assert crit, "critical failure must be recorded"
        assert any(f["id"] == "preflop_aa_first_act" for f in r["failures"])
        assert r["passed"] < r["total"]

    def test_empty_scenarios_returns_safe_defaults(self, monkeypatch, tmp_path):
        """No scenarios → safe-default dict (pass_rate 1.0, zero counts), matching
        run_decision_tests_detail's empty-path contract so the gate never blocks
        on a missing scenarios file."""
        _write_scenarios_file(monkeypatch, tmp_path, [])
        r = dt.run_decision_tests_sprt_aggregate("fake")
        assert r["total"] == 0
        assert r["pass_rate"] == 1.0
        assert r["critical_failures"] == []
        assert r["sprt_decisions"] == []


# ──────────────────────────────────────────────
# B3. tool_gates flag default + OFF-path parity
#     — DECISION_TEST_SPRT_ENABLED defaults False so the gate uses the classic
#       run_decision_test_details path (zero-regression). This pins the default.
# ──────────────────────────────────────────────

def test_decision_test_sprt_flag_defaults_off():
    """The SPRT gate is opt-in and defaults OFF (zero-regression). Flipping it
    is a deliberate gray-run decision, not the default."""
    import tool_gates
    assert tool_gates.DECISION_TEST_SPRT_ENABLED is False


# ──────────────────────────────────────────────
# C. Behavior fingerprints
# ──────────────────────────────────────────────


def _action_log(bot_idx, n_community, action, pot=1000):
    """Build a single log entry where bot_idx took `action` with n_community cards."""
    return {
        "output": {
            "display": {
                "last_action": {"player_id": bot_idx, "action": action},
                "public_cards": list(range(n_community)),
                "pot": pot,
            }
        }
    }


def _game(*logs):
    return {"logs": list(logs)}


class TestFingerprints:
    def test_fingerprint_same_bot_zero_distance(self):
        games = [_game(_action_log(0, 3, 400)), _game(_action_log(0, 5, 0))]
        fp = ra.extract_behavior_fingerprint(games, 0)
        assert ra.fingerprint_distance(fp, fp) == pytest.approx(0.0)

    def test_fingerprint_different_bots_high_distance(self):
        # Aggressive: all raises on the flop.
        agg_games = [
            _game(_action_log(0, 3, 600), _action_log(0, 3, 800)) for _ in range(5)
        ]
        # Passive: all calls/folds on the flop.
        pas_games = [
            _game(_action_log(0, 3, 0), _action_log(0, 3, -1)) for _ in range(5)
        ]
        fp_agg = ra.extract_behavior_fingerprint(agg_games, 0)
        fp_pas = ra.extract_behavior_fingerprint(pas_games, 0)
        dist = ra.fingerprint_distance(fp_agg, fp_pas)
        assert 0.0 <= dist <= 1.0
        assert dist > 0.5

    def test_fingerprint_per_street_freq_normalized(self):
        # Single game: two raise actions on the flop only.
        games = [_game(_action_log(0, 3, 600), _action_log(0, 3, 800))]
        fp = ra.extract_behavior_fingerprint(games, 0)
        flop = fp["per_street_freq"]["flop"]
        assert flop["raise"] == pytest.approx(1.0)
        assert flop["call"] == pytest.approx(0.0)
        assert flop["fold"] == pytest.approx(0.0)
        # Other streets have no actions → all zeros.
        for s in ("preflop", "turn", "river"):
            assert sum(fp["per_street_freq"][s].values()) == pytest.approx(0.0)

    def test_fingerprint_aggression_factor(self):
        # 2 raises + 1 allin vs 1 call ⇒ (2+1)/(1+1) = 1.5
        games = [_game(
            _action_log(0, 3, 600),   # raise
            _action_log(0, 3, -2),    # allin
            _action_log(0, 3, 0),     # call
            _action_log(0, 4, 800),   # raise
        )]
        fp = ra.extract_behavior_fingerprint(games, 0)
        assert fp["aggression_factor"] == pytest.approx((2 + 1) / (1 + 1))

    def test_fingerprint_empty_games_safe(self):
        fp = ra.extract_behavior_fingerprint([], 0)
        assert fp["total_actions"] == 0
        assert fp["aggression_factor"] is None
        assert fp["vpip"] is None
        assert fp["call_down_rate"] is None
        # Zero fingerprint vs itself ⇒ 0.0; zero vs non-zero ⇒ 1.0.
        assert ra.fingerprint_distance(fp, fp) == pytest.approx(0.0)
        real = ra.extract_behavior_fingerprint(
            [_game(_action_log(0, 3, 600))], 0
        )
        assert ra.fingerprint_distance(fp, real) == pytest.approx(1.0)

    def test_extract_street_patterns_unchanged(self):
        """Regression: the string summary format is unchanged."""
        games = [_game(_action_log(0, 3, 400, pot=1000))]
        summary = ra.extract_street_patterns(games, 0)
        # Flop line must be present and mention raise=100%.
        assert "Flop:" in summary
        assert "raise=100%" in summary


# ──────────────────────────────────────────────
# D. Generator early-stop (tool_eval)
# ──────────────────────────────────────────────


@pytest.fixture
def mock_ui():
    ui = MagicMock()
    ui.log_history = MagicMock()
    return ui


@pytest.fixture(autouse=True)
def _mock_precommit_semantic(monkeypatch):
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "default")

    async def _fake_semantic(v, source_v, matchups, master_plan, ui):
        return {
            "win_pattern_analysis": "",
            "top_opponent_assessment": "",
            "regression_semantics": "safe",
            "recommended_action": "proceed",
            "confidence": "low",
        }

    import audit_agents
    monkeypatch.setattr(audit_agents, "_run_precommit_semantic", _fake_semantic)


@pytest.fixture
def patch_checkpoint_file(monkeypatch, tmp_path):
    import evolution_infra
    ckpt_path = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_path)
    return ckpt_path


@pytest.fixture
def fake_bots(tmp_path, monkeypatch):
    bots_dir = tmp_path / "bots"
    bots_dir.mkdir()
    for name in ("national_v99", "national_v98", "national_v50"):
        d = bots_dir / name
        d.mkdir()
        (d / "main.py").write_text("# fake bot")
    monkeypatch.setattr("tool_eval._bot_main", lambda name: bots_dir / name / "main.py")
    return bots_dir


@pytest.fixture
def fake_opponents(monkeypatch):
    ops = [
        {"name": "national_v98", "reason": "parent"},
        {"name": "national_v50", "reason": "top_opponent"},
    ]
    monkeypatch.setattr("tool_eval._select_precommit_opponents", lambda _v, _sv: ops)
    return ops


def _seed_passing_checkpoint():
    tool_eval.write_pipeline_checkpoint(
        99, 98, "critic_checked",
        gate_results={
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
            "review": {
                "approved": True,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
            },
            "critic": {
                "approved": True,
                "score": 7,
                "llm_invoked": True,
                "critic_llm_executed": True,
                "schema_valid": True,
            },
        },
        precommit_attempt=0,
    )


def _common_patches(monkeypatch, mock_ui):
    monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
    monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
    monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)


def _decode(result):
    return json.loads(result["content"][0]["text"])


class TestPrecommitGeneratorEarlyStop:
    @pytest.mark.asyncio
    async def test_precommit_no_early_stop_when_flag_off(
        self, monkeypatch, fake_bots, fake_opponents, mock_ui, patch_checkpoint_file
    ):
        """Flag OFF: the old fixed-collect mirror_battle path runs unchanged.

        The classic test fixture returns a 4-tuple ([2,6], 0, n, None) for the
        parent — flag OFF must consume it exactly as before and produce a
        lost_to_parent blocker with no cs_meta.
        """
        monkeypatch.setattr(tool_eval, "PRECOMMIT_SEQUENTIAL_EARLY_STOP", False)
        # Patch mirror_battle to return the 4-tuple shape (parent loses 2-6).
        def fake_mirror(a, b, n_games=1, verbose=False, save_log=False):
            if "national_v98" in b:
                return ([2, 6], 0, n_games, None)
            return ([3, 3], 0, n_games, None)
        _battle_module = sys.modules["engine.battle"]
        monkeypatch.setattr(_battle_module, "mirror_battle", fake_mirror)

        _seed_passing_checkpoint()
        _common_patches(monkeypatch, mock_ui)
        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)
        assert data["passed"] is False
        parent_matchup = next(m for m in data["matchups"] if m["opponent"] == "national_v98")
        # Flag OFF path never sets cs_meta.
        assert parent_matchup.get("cs_meta") is None
        # lost_to_parent blocker present (regression parity).
        assert any(b.get("reason") == "lost_to_parent" for b in data["blockers"])

    @pytest.mark.asyncio
    async def test_precommit_wins_losses_reconstructed_from_generator(
        self, monkeypatch, fake_bots, fake_opponents, mock_ui, patch_checkpoint_file
    ):
        """Flag ON: W/L/D reconstructed from the generator net-chips stream."""
        monkeypatch.setattr(tool_eval, "PRECOMMIT_SEQUENTIAL_EARLY_STOP", True)
        _battle_module = sys.modules["engine.battle"]
        stream = [-100, 200, 0, 500, -300, 700, 0, -50]  # 8 pairs
        def fake_gen(*a, **k):
            for x in stream:
                yield x
        monkeypatch.setattr(_battle_module, "mirror_battle_generator", fake_gen)
        # mirror_battle must not be called on the generator path.
        def _boom(*a, **k):
            raise AssertionError("mirror_battle must not run on generator path")
        monkeypatch.setattr(_battle_module, "mirror_battle", _boom)

        _seed_passing_checkpoint()
        _common_patches(monkeypatch, mock_ui)
        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)
        # Parent matchup W/L/D derived from stream signs.
        parent = next(m for m in data["matchups"] if m["opponent"] == "national_v98")
        assert parent["wins"] == 3     # 200, 500, 700
        assert parent["losses"] == 3   # -100, -300, -50
        assert parent["draws"] == 2    # 0, 0
        assert parent["n_played"] == 8
        assert parent["net_chips"] == stream

    @pytest.mark.asyncio
    async def test_precommit_early_stop_on_reject(
        self, monkeypatch, fake_bots, fake_opponents, mock_ui, patch_checkpoint_file
    ):
        """Parent CS DECIDE_REJECT breaks the generator early and blocks.

        The default R=NET_CHIPS_RANGE (80000) makes the anytime-valid half-width
        wider than the bounded value range at n≤16, so real-world early-stop
        under the conservative fixed R only triggers for very aggressive /
        high-variance bots. To unit-test the *mechanism* deterministically, we
        shrink the effective R (via tool_eval._CS_R) so a strongly negative
        stream crosses the reject boundary well within n_games.
        """
        monkeypatch.setattr(tool_eval, "PRECOMMIT_SEQUENTIAL_EARLY_STOP", True)
        # Shrink R so a -30000 mean crosses ci_hi < -2000 inside n_games=16.
        # At t=8 half-width ≈ R*sqrt(ln(π²·64/0.3)/16) ≈ R*0.6916; with R=4000
        # that is ≈ 2766, so mean -30000 ⇒ ci_hi ≈ -32766 < -2000 ⇒ DECIDE_REJECT
        # at the very first sample. Use R=4000 to guarantee a fast early stop.
        monkeypatch.setattr(tool_eval, "_CS_R", 4000.0)
        _battle_module = sys.modules["engine.battle"]
        full = [-30000] * 16
        parent_yielded = {"n": 0}
        def fake_gen(a, b, *a2, **k):
            # Only the parent matchup early-stops; count its yields specifically.
            counter = parent_yielded if "national_v98" in b else None
            for x in full:
                if counter is not None:
                    counter["n"] += 1
                yield x
        monkeypatch.setattr(_battle_module, "mirror_battle_generator", fake_gen)

        def _boom(*a, **k):
            raise AssertionError("mirror_battle must not run on generator path")
        monkeypatch.setattr(_battle_module, "mirror_battle", _boom)

        _seed_passing_checkpoint()
        _common_patches(monkeypatch, mock_ui)
        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 16})
        data = _decode(result)
        assert data["passed"] is False
        parent = next(m for m in data["matchups"] if m["opponent"] == "national_v98")
        meta = parent.get("cs_meta")
        assert meta is not None
        assert meta["decision"] == "DECIDE_REJECT"
        assert meta["early_stopped"] is True
        # Did not consume the full 16-stream.
        assert meta["n"] < 16
        # The parent drained exactly meta["n"] pairs before the CS broke the loop.
        assert parent_yielded["n"] == meta["n"]
        # lost_to_parent blocker fired.
        assert any(b.get("reason") == "lost_to_parent" for b in data["blockers"])
        # And early-stop is NOT flagged incomplete.
        assert not any(
            b.get("reason") == "incomplete_or_timeout" and b.get("opponent") == "national_v98"
            for b in data["blockers"]
        )

    @pytest.mark.asyncio
    async def test_precommit_n_played_not_incomplete_on_early_stop(
        self, monkeypatch, fake_bots, fake_opponents, mock_ui, patch_checkpoint_file
    ):
        """Early-stop on parent: no incomplete_or_timeout blocker despite n<16."""
        monkeypatch.setattr(tool_eval, "PRECOMMIT_SEQUENTIAL_EARLY_STOP", True)
        _battle_module = sys.modules["engine.battle"]
        full = [-30000] * 16
        def fake_gen(*a, **k):
            for x in full:
                yield x
        monkeypatch.setattr(_battle_module, "mirror_battle_generator", fake_gen)

        def _boom(*a, **k):
            raise AssertionError("mirror_battle must not run")
        monkeypatch.setattr(_battle_module, "mirror_battle", _boom)
        _seed_passing_checkpoint()
        _common_patches(monkeypatch, mock_ui)
        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 16})
        data = _decode(result)
        parent_blockers = [
            b for b in data["blockers"]
            if b.get("opponent") == "national_v98" or "national_v98" in str(b)
        ]
        # No incomplete blocker on the parent matchup.
        assert not any(b.get("reason") == "incomplete_or_timeout" for b in parent_blockers)

    @pytest.mark.asyncio
    async def test_precommit_aggregate_gate_unchanged(
        self, monkeypatch, fake_bots, fake_opponents, mock_ui, patch_checkpoint_file
    ):
        """Aggregate gate still uses paired_bootstrap_ci over the pooled stream."""
        monkeypatch.setattr(tool_eval, "PRECOMMIT_SEQUENTIAL_EARLY_STOP", True)
        _battle_module = sys.modules["engine.battle"]
        # Both opponents yield mild streams; aggregate must not regress.
        def fake_gen(a, b, *a2, **k):
            if "national_v98" in b:
                for x in [100, -100, 100, -100, 100, -100, 100, -100]:
                    yield x
            else:
                for x in [50, -50, 50, -50, 50, -50, 50, -50]:
                    yield x
        monkeypatch.setattr(_battle_module, "mirror_battle_generator", fake_gen)

        def _boom(*a, **k):
            raise AssertionError("mirror_battle must not run")
        monkeypatch.setattr(_battle_module, "mirror_battle", _boom)
        _seed_passing_checkpoint()
        _common_patches(monkeypatch, mock_ui)
        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)
        # Mild symmetric streams ⇒ passes (no aggregate regression blocker).
        assert data["passed"] is True
        assert not any(
            b.get("reason") == "aggregate_precommit_regression" for b in data["blockers"]
        )


# ──────────────────────────────────────────────
# E. Feature-flag default + bootstrap regression
# ──────────────────────────────────────────────


class TestFeatureFlagAndRegression:
    def test_precommit_feature_flag_default_on(self):
        assert tool_eval.PRECOMMIT_SEQUENTIAL_EARLY_STOP is True

    def test_paired_bootstrap_ci_unchanged(self):
        """paired_bootstrap_ci still produces a sensible CI (regression)."""
        lo, hi = paired_bootstrap_ci([1, 2, 3, 4, 5], seed=12345)
        assert lo < hi
        assert 1.0 <= lo <= 3.0
        assert 3.0 <= hi <= 5.0
