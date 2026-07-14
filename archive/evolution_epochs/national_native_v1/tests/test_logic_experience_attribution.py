"""ARCHIVED national_native_v1 tests for experience-pool Ratchet retire.

Verifies:
  - record_lesson_outcome accumulates attribution counters
  - reconcile_lesson_outcomes uses rating_delta (< 0 = hurt), NOT precommit_passed
    (the fatal INERT flaw the original plan had)
  - retire_lessons respects N_min=30 (does NOT retire below the floor)
  - score_lesson reuses research_governance's score_candidate ĉ formula
  - the stale battle.py stderr entry in experience_pool.md was updated (dogfood)
"""

import json

import pytest

from glicko2 import Glicko2Player
import evolution_infra
import experience_attribution as ea


def _results_dir():
    """Access RESULTS_DIR via the module attribute at call time so conftest's
    monkeypatch (which patches evolution_infra.RESULTS_DIR) takes effect.
    A module-level `from evolution_infra import RESULTS_DIR` would bind the
    original production path at import/collection time, before the autouse
    fixture runs."""
    return evolution_infra.RESULTS_DIR


# ── Helpers ──

def _write_attribution(attrib):
    """Write the sidecar directly (bypassing record_lesson_outcome) for setup."""
    rd = _results_dir()
    rd.mkdir(parents=True, exist_ok=True)
    with open(rd / "experience_attribution.json", "w", encoding="utf-8") as f:
        json.dump(attrib, f)


def _read_attribution():
    p = _results_dir() / "experience_attribution.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


@pytest.fixture(autouse=True)
def _clean_attribution():
    """Ensure each test starts with an empty sidecar.

    conftest's isolate_state fixture monkeypatches evolution_infra.RESULTS_DIR
    to a tmp dir, and experience_attribution resolves the sidecar path at call
    time via _attribution_file(), so this isolation is effective.
    """
    p = _results_dir() / "experience_attribution.json"
    if p.exists():
        p.unlink()
    yield
    if p.exists():
        p.unlink()


# ── Test: record_lesson_outcome accumulates ──

class TestRecordLessonOutcomeAccumulates:
    """Verify record_lesson_outcome accumulates attributed_hurt/help/trials."""

    def test_accumulates_help_on_positive_delta(self):
        ea.record_lesson_outcome("lesson_A", rating_delta=+50.0, source_gen=100)
        ea.record_lesson_outcome("lesson_A", rating_delta=+30.0, source_gen=100)
        ea.record_lesson_outcome("lesson_A", rating_delta=-10.0, source_gen=100)

        attrib = _read_attribution()
        entry = attrib["lesson_A"]
        assert entry["trials"] == 3
        assert entry["attributed_help"] == 2  # +50, +30
        assert entry["attributed_hurt"] == 1   # -10
        assert entry["last_rating_delta"] == -10.0
        assert entry["status"] == "active"

    def test_accumulates_hurt_on_negative_delta(self):
        ea.record_lesson_outcome("lesson_B", rating_delta=-80.0, source_gen=101)
        attrib = _read_attribution()
        entry = attrib["lesson_B"]
        assert entry["attributed_hurt"] == 1
        assert entry["attributed_help"] == 0
        assert entry["source_gen"] == 101

    def test_zero_delta_is_neutral(self):
        """rating_delta == 0 should count as a trial but neither help nor hurt."""
        ea.record_lesson_outcome("lesson_C", rating_delta=0.0, source_gen=102)
        attrib = _read_attribution()
        entry = attrib["lesson_C"]
        assert entry["trials"] == 1
        assert entry["attributed_help"] == 0
        assert entry["attributed_hurt"] == 0

    def test_fallback_hurt_verdict_without_delta(self):
        """When rating_delta is absent, hurt_verdict is the fallback signal."""
        ea.record_lesson_outcome("lesson_D", hurt_verdict="hurt", source_gen=103)
        ea.record_lesson_outcome("lesson_D", hurt_verdict="helped", source_gen=103)
        attrib = _read_attribution()
        entry = attrib["lesson_D"]
        assert entry["attributed_hurt"] == 1
        assert entry["attributed_help"] == 1
        assert entry["trials"] == 2

    def test_none_lesson_id_is_noop(self):
        ea.record_lesson_outcome(None, rating_delta=-10.0)
        assert _read_attribution() == {}


# ── Test: reconcile uses rating_delta (NOT precommit_passed) ──

class TestReconcileUsesRatingDelta:
    """The fatal flaw in the original plan: hooking on precommit_passed makes
    won ALWAYS True (commit gate requires it). The fix uses rating_delta < 0
    as the hurt signal. Verify reconcile derives the signal from rating_delta."""

    def test_reconcile_negative_delta_accumulates_hurt(self):
        """A lesson whose source-gen bot converged with a NEGATIVE rating
        delta must accumulate hurt (this is what makes retire non-INERT)."""
        # Seed a lesson pointing at gen 100
        _write_attribution({
            "lesson_X": {
                "source_gen": 100,
                "source_v": 99,
                "attributed_hurt": 0,
                "attributed_help": 0,
                "trials": 0,
                "status": "active",
            }
        })

        # Bot converged with r LOWER than its source => rating_delta < 0 => hurt
        ratings = {
            "national_v100": Glicko2Player(r=1450.0, rd=45.0),
            "national_v99": Glicko2Player(r=1520.0, rd=50.0),
        }
        bot_stats = {"national_v100": {"games": 150}}

        ea.reconcile_lesson_outcomes(ratings, bot_stats)

        attrib = _read_attribution()
        entry = attrib["lesson_X"]
        # rating_delta = 1450 - 1520 = -70 < 0 => hurt
        assert entry["attributed_hurt"] == 1
        assert entry["attributed_help"] == 0
        assert entry["last_rating_delta"] == -70.0
        assert entry["last_reconciled_gen"] == 100  # idempotency marker set

    def test_reconcile_positive_delta_accumulates_help(self):
        _write_attribution({
            "lesson_Y": {
                "source_gen": 100,
                "attributed_hurt": 0, "attributed_help": 0,
                "trials": 0, "status": "active",
            }
        })
        ratings = {
            "national_v100": Glicko2Player(r=1600.0, rd=40.0),
        }
        bot_stats = {"national_v100": {"games": 200}}
        ea.reconcile_lesson_outcomes(ratings, bot_stats)
        entry = _read_attribution()["lesson_Y"]
        # rating_delta = 1600 - 1500 = +100 > 0 => help
        assert entry["attributed_help"] == 1
        assert entry["attributed_hurt"] == 0
        assert entry["last_rating_delta"] == 100.0

    def test_reconcile_skips_not_converged(self):
        """High-RD bot (not converged) must NOT be reconciled yet."""
        _write_attribution({
            "lesson_Z": {
                "source_gen": 100,
                "attributed_hurt": 0, "attributed_help": 0,
                "trials": 0, "status": "active",
            }
        })
        ratings = {
            "national_v100": Glicko2Player(r=1450.0, rd=200.0),  # high RD
        }
        bot_stats = {"national_v100": {"games": 200}}
        ea.reconcile_lesson_outcomes(ratings, bot_stats)
        entry = _read_attribution()["lesson_Z"]
        assert entry["attributed_hurt"] == 0
        assert "last_reconciled_gen" not in entry

    def test_reconcile_is_idempotent(self):
        """Once a gen is reconciled, a second call must NOT re-accumulate."""
        _write_attribution({
            "lesson_W": {
                "source_gen": 100,
                "attributed_hurt": 0, "attributed_help": 0,
                "trials": 0, "status": "active",
            }
        })
        ratings = {"national_v100": Glicko2Player(r=1450.0, rd=45.0)}
        bot_stats = {"national_v100": {"games": 150}}

        ea.reconcile_lesson_outcomes(ratings, bot_stats)
        ea.reconcile_lesson_outcomes(ratings, bot_stats)  # second call

        entry = _read_attribution()["lesson_W"]
        assert entry["attributed_hurt"] == 1  # NOT 2 — idempotent
        assert entry["trials"] == 1

    def test_reconcile_empty_sidecar_is_noop(self):
        """No sidecar => reconcile returns early without error."""
        assert ea.reconcile_lesson_outcomes({}, {}) is False


# ── Test: retire respects N_min=30 ──

class TestRetireRespectsNMin30:
    """The Ratchet ablation showed N_min=20 produces -0.019 active HARM. The
    floor MUST be 30. Verify nothing retires below 30 trials even with
    terrible ĉ."""

    def test_does_not_retire_below_30_trials(self):
        """29 trials, all hurt (ĉ = -1.0) must NOT retire — below the floor."""
        _write_attribution({
            "lesson_under": {
                "source_gen": 100,
                "attributed_hurt": 29, "attributed_help": 0,
                "trials": 29, "status": "active",
            }
        })
        retired = ea.retire_lessons()
        assert retired == []  # below N_min=30
        entry = _read_attribution()["lesson_under"]
        assert entry["status"] == "active"

    def test_retires_at_30_trials_with_low_score(self):
        """30 trials, all hurt (ĉ = -1.0 <= -0.10) MUST retire."""
        _write_attribution({
            "lesson_at_floor": {
                "source_gen": 100,
                "attributed_hurt": 30, "attributed_help": 0,
                "trials": 30, "status": "active",
            }
        })
        retired = ea.retire_lessons()
        assert "lesson_at_floor" in retired
        entry = _read_attribution()["lesson_at_floor"]
        assert entry["status"] == "retired"
        assert "low_score_after_30_trials" in entry["retired_reason"]

    def test_does_not_retire_positive_score(self):
        """Even with many trials, a positive ĉ (more help than hurt) is NOT retired."""
        _write_attribution({
            "lesson_good": {
                "source_gen": 100,
                "attributed_hurt": 5, "attributed_help": 40,
                "trials": 45, "status": "active",
            }
        })
        retired = ea.retire_lessons()
        assert retired == []
        assert _read_attribution()["lesson_good"]["status"] == "active"

    def test_does_not_retire_borderline_score_above_tau(self):
        """ĉ just above -0.10 (e.g. -0.05) must NOT retire."""
        # trials=40, help=18, hurt=20 => ĉ = (18-20)/40 = -0.05 > -0.10
        _write_attribution({
            "lesson_borderline": {
                "source_gen": 100,
                "attributed_hurt": 20, "attributed_help": 18,
                "trials": 40, "status": "active",
            }
        })
        retired = ea.retire_lessons()
        assert retired == []
        assert _read_attribution()["lesson_borderline"]["status"] == "active"

    def test_retire_just_below_tau(self):
        """ĉ = -0.125 (<= -0.10) with >=30 trials MUST retire."""
        # trials=40, help=15, hurt=20 => ĉ = (15-20)/40 = -0.125 <= -0.10
        _write_attribution({
            "lesson_just_bad": {
                "source_gen": 100,
                "attributed_hurt": 20, "attributed_help": 15,
                "trials": 40, "status": "active",
            }
        })
        retired = ea.retire_lessons()
        assert "lesson_just_bad" in retired

    def test_cannot_lower_floor_below_30(self):
        """Even if caller passes min_trials=10, the RETIRE_N_MIN=30 floor wins."""
        _write_attribution({
            "lesson_floor": {
                "source_gen": 100,
                "attributed_hurt": 20, "attributed_help": 0,
                "trials": 20, "status": "active",  # ĉ=-1.0 but trials < 30
            }
        })
        # Caller tries to be aggressive — the floor must protect.
        retired = ea.retire_lessons(min_trials=10)
        assert retired == []  # 30 floor enforced
        assert _read_attribution()["lesson_floor"]["status"] == "active"


# ── Test: score_lesson uses research_governance formula ──

class TestScoreLessonUsesResearchGovernanceFormula:
    """score_lesson must return the SAME value as research_governance.score_candidate
    for the same counters (it imports and delegates to it)."""

    def test_score_matches_research_governance(self):
        from research_governance import score_candidate
        entry = {"attributed_hurt": 3, "attributed_help": 7, "trials": 10}
        assert ea.score_lesson(entry) == score_candidate(entry)
        # (7-3)/10 = 0.4
        assert ea.score_lesson(entry) == 0.4

    def test_score_zero_trials_denominator_is_one(self):
        """trials=0 => denominator max(0,1)=1 (no division by zero)."""
        entry = {"attributed_hurt": 0, "attributed_help": 0, "trials": 0}
        assert ea.score_lesson(entry) == 0.0  # (0-0)/1

    def test_score_negative_when_more_hurt(self):
        entry = {"attributed_hurt": 10, "attributed_help": 2, "trials": 12}
        # (2-10)/12 = -0.667
        assert ea.score_lesson(entry) < 0
        assert ea.score_lesson(entry) == pytest.approx(-8/12)


# ── Test: dogfood — stale stderr entry updated ──

class TestDriftEntryUpdated:
    """Verify the stale 'battle.py stderr unreadable' entries in
    experience_pool.md were updated (A1 fix landed).

    NOTE: reads the PRODUCTION experience_pool.md directly (not the
    monkeypatched EXPERIENCE_FILE, which conftest redirects to a stub),
    because this test is a dogfood check on the real doc.
    """

    def _production_pool(self):
        from pathlib import Path
        proj = Path(__file__).resolve().parents[2]
        return (proj / "web" / "core" / "experience_pool.md").read_text()

    def _production_prompt(self, name):
        from pathlib import Path
        proj = Path(__file__).resolve().parents[2]
        return (proj / "web" / "core" / "prompts" / name).read_text()

    def test_pool_no_longer_claims_stderr_unreadable(self):
        content = self._production_pool()
        # The old stale claim must be gone...
        assert "reads ONLY stdout" not in content
        # ...and the highest-ROI-unblock framing must be replaced.
        assert "HIGHEST-ROI UNBLOCK" not in content
        # The national-native epoch reset may prune old resolved markers; the
        # regression is the stale claim returning, not the absence of old notes.
        assert "battle.py stderr unreadable" not in content

    def test_consolidator_prompt_has_demote_stale_rule(self):
        prompt = self._production_prompt("experience_consolidator.md")
        assert "Demote stale directions" in prompt
        assert "no WR-lift" in prompt
