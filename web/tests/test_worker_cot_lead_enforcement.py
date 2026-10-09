"""Replay regression: the CoT lead/review authority split on real v526-v530
runtime verdicts (root fix 2026-10-08).

Every fixture below is embedded verbatim (or as an explicit diff-facts
projection) from the read-only runtime logs under
``/home/ubuntu/pok1/.evolution_pok/web/core/results/v52{6,8,9}/…`` — the
tests never import the runtime directory.

Two decisive acceptance families (per the approved design):

* (a) the recorded false-positive chains (v526 ×2, v529 ×2, v530 ×1 — the
  attempts whose CoT verdicts triggered `_cot_inconsistency_blocks_task`
  hard blocks and `_reset_target_files_to_source` rollbacks) must ALL come
  back ``block=False`` from ``review_worker_cot_lead``;
* (b) constructed true violations (real Tuner editing control flow; a
  side-effect claim confirmed by an added ``print(`` line; a task-mismatch
  claim confirmed by all-unchanged targets) must STILL block.

The seam-level contract (block ⇒ rollback + focus areas; downgrade ⇒
advisory event, no rollback) is driven through
``agent_workers._handle_worker_cot_inconsistency`` with a stub UI.
"""

import difflib

import pytest

import tool_planning  # noqa: F401  (parent-first import order)
import worker_role_policy as wrp


# ══════════════════════════════════════════════════════════════════════
# Embedded runtime fixtures (extracted read-only; see module docstring)
# ══════════════════════════════════════════════════════════════════════

V526_ARCH_TASK = {
    "worker_id": "auto_runtime_architecture_policy_py",
    "role": "Opponent Modeler",
    "task_kind": "quality_repair",
    "repair_blocker": "runtime_architecture",
    "target_files": ["policy.py"],
    "must_change_files": ["policy.py"],
    "worker_prompt": (
        "Repair contract: runtime_architecture\n"
        "- Architecture focus: `terminal_response`\n"
        "- Writable candidate file: `policy.py` only.\n"
    ),
}

# v526 attempt 3 verdict (fenced json, verbatim) — role Opponent Modeler.
V526_ARCH_COT_ATTEMPT3 = {
    "worker_id": 1,
    "cot_consistent": False,
    "discrepancies": [
        "Worker metadata claims `_opponent_context_profile` was changed, but "
        "the actual +25-line diff only modifies `get_baseline_decision`; the "
        "existing profile function is consumed, not edited."
    ],
    "logical_contradictions": [],
    "boundary_violations": [],
    "focus_areas": [
        "Confirm `get_baseline_decision` is the only modified function and "
        "correct the Worker's `changed_functions` metadata.",
        "Verify the combined confidence product and prior-centered "
        "fold/aggression credits provide the claimed confidence-gated causal "
        "influence without giving sparse evidence credit.",
    ],
}

# v526 attempt 3 diff — structural (+25 lines: new code), numbers_only=False.
V526_ARCH_BEFORE_ATTEMPT3 = '''    kinds = set(legal.get("policy_kinds", ()))
    if "fold" in kinds and _fold_locks_match_win(context):
        return {"kind": "fold"}
    fraction = _polarized_raise_fraction(context, equity)
    raise_frequency_bonus = 0.18 * context_fold_signal
    if fraction is not None and (
        to_call == 0
        or equity >= pot_odds + 0.22 - raise_frequency_bonus
    ):
        raised = _raise_intent(context, fraction, adaptation_scale=0.5)
        if raised is not None:
            return raised
'''
V526_ARCH_AFTER_ATTEMPT3 = '''    context_fold_signal = context_profile["action_profile_fold_signal"]
    # Reducer-owned action_profile telemetry: both gates must clear before the
    # exploit leaves the parent prior.  Sparse or malformed evidence is neutral.
    profile_confidence = _bounded(
        opponent.get("confidence"), 0.0, 1.0
    ) * _bounded(context_profile["confidence"], 0.0, 1.0)
    profile_fold_rate = _bounded(
        context_profile["fold_to_raise"], 0.0, 1.0, FOLD_TO_RAISE_PRIOR
    )
    profile_counterfactual_credit = max(
        0.0,
        0.04
        * profile_confidence
        * (profile_fold_rate - FOLD_TO_RAISE_PRIOR),
    )
    kinds = set(legal.get("policy_kinds", ()))
    if "fold" in kinds and _fold_locks_match_win(context):
        return {"kind": "fold"}
    fraction = _polarized_raise_fraction(context, equity)
    raise_frequency_bonus = (
        0.18 * context_fold_signal + profile_counterfactual_credit
    )
    if fraction is not None and (
        to_call == 0
        or equity >= pot_odds + 0.22 - raise_frequency_bonus
    ):
        raised = _raise_intent(context, fraction, adaptation_scale=0.5)
        if raised is not None:
            return raised
'''

# v526 attempt 4 verdict (single-line json, verbatim) — role Opponent Modeler.
# Under the OLD code this attempt hard-blocked via the runtime-side-effect
# regex matching the word "telemetry" inside focus_areas.
V526_ARCH_COT_ATTEMPT4 = {
    "worker_id": 1,
    "cot_consistent": False,
    "discrepancies": [
        "Claimed a bounded ±0.18 equity-threshold shift, but the facing-bet "
        "raise coefficient changed from 0.18 to 0.45; the diff does not "
        "establish that the action-profile signal is bounded to produce the "
        "claimed ±0.18 maximum shift.",
        "Claimed the facing-bet threshold now uses confidence-gated "
        "`action_profile_fold_signal`, but the diff only changes the "
        "multiplier on the already-consumed `context_fold_signal`; it does "
        "not show newly added confidence gating or sparse-evidence neutrality."
    ],
    "logical_contradictions": [
        "Worker stated the bounded shift is ±0.18 while implementing a 0.45 "
        "coefficient, which contradicts the stated bound unless the signal is "
        "independently bounded to at most ±0.4, but that bound is not visible "
        "in the diff."
    ],
    "boundary_violations": [],
    "focus_areas": [
        "Inspect `_opponent_context_profile` to verify "
        "`action_profile_fold_signal` is confidence-gated, bounded, and "
        "neutral for missing/sparse evidence; confirm whether its range makes "
        "0.45 * signal exactly bounded by ±0.18.",
        "Confirm the changed facing-bet raise threshold cannot make the "
        "opponent signal causally excessive or violate the intended ±0.18 "
        "shift.",
        "Verify that the action-profile signal affects a legal "
        "socket-visible intent, not only refinement telemetry, because the "
        "shown refinement branch emits telemetry only when another adjustment "
        "already changes the intent.",
        "Check that the new telemetry reason is retained in the expected "
        "bounded length and does not alter decision semantics beyond the "
        "claimed opponent-model repair."
    ],
}

# v526 attempt 4 diff — structural (new statements added), numbers_only=False.
V526_ARCH_BEFORE_ATTEMPT4 = '''    fraction = _polarized_raise_fraction(context, equity)
    raise_frequency_bonus = 0.18 * context_fold_signal
    if fraction is not None and (
        to_call == 0
        or equity >= pot_odds + 0.22 - raise_frequency_bonus
    ):
'''
V526_ARCH_AFTER_ATTEMPT4 = '''    fraction = _polarized_raise_fraction(context, equity)
    raise_frequency_bonus = 0.45 * context_fold_signal
    if fraction is not None and (
        to_call == 0
        or equity >= pot_odds + 0.22 - raise_frequency_bonus
    ):
'''
V526_ARCH_BEFORE_ATTEMPT4_B = '''    baseline_equity = _refinement_prior_equity(context, hole, board)
    fraction = _polarized_raise_fraction(context, baseline_equity)
    if baseline.get("kind") == "raise" and fraction is not None:
'''
V526_ARCH_AFTER_ATTEMPT4_B = '''    baseline_equity = _refinement_prior_equity(context, hole, board)
    fraction = _polarized_raise_fraction(context, baseline_equity)
    opponent = context.get("opponent") or {}
    action_signal = _opponent_context_profile(
        opponent,
        (context.get("hand") or {}).get("street"),
    )["action_profile_fold_signal"]
    if baseline.get("kind") == "raise" and fraction is not None:
'''

V529_PRECOMMIT_TASK = {
    "worker_id": "auto_precommit_repair_policy_py",
    "role": "Strategic Regression Repair Architect",
    "task_kind": "precommit_repair",
    "repair_blocker": "precommit_regression",
    "target_files": ["policy.py"],
    "must_change_files": ["policy.py"],
    "worker_prompt": (
        "This is one file-scoped precommit regression repair from a failed "
        "native national TCP final gate.\nTarget file: `policy.py`\n"
    ),
}

# v529 attempt 1 verdict (verbatim) — the LLM applied the old prompt's
# Architect constants rule and demanded a Tuner escalation.
V529_PRECOMMIT_COT = {
    "worker_id": 1,
    "cot_consistent": False,
    "discrepancies": [],
    "logical_contradictions": [],
    "boundary_violations": [
        "Architect role changed numeric policy thresholds (0.20 to 0.28, "
        "0.25 to 0.12), while the role boundary restricts it to structural "
        "changes only"
    ],
    "focus_areas": [
        "Reviewer should scrutinize whether the tightened checked-through "
        "gate and evidence thresholds make the river air-raise branch "
        "sufficiently reachable",
        "Reviewer should verify downstream EV refinement can still override "
        "or bound the retained exploit safely",
        "Confirm the numeric threshold changes were intended for this "
        "Architect role rather than requiring a subordinate Tuner task"
    ],
}

# v529 attempt 1 diff excerpt (verbatim head) — structural: comment rewrites
# plus `if to_call > 0.50 * pot:` → `if to_call > 0.0:` and a widened
# multi-condition gate. numbers_only=False.
V529_PRECOMMIT_BEFORE = '''        # Bound the bluff commitment to a small pot fraction; larger facing
        # bets stay passive and are left to the unchanged EV gate downstream.
        if to_call > 0.50 * pot:
            return False
'''
V529_PRECOMMIT_AFTER = '''        # A regression-safe river exploit requires a checked-through spot.
        # Raising air after the opponent has already committed river chips
        # turns noisy terminal fold evidence into an unbounded caller-range
        # mistake; those spots remain passive here.
        if to_call > 0.0:
            return False
'''

V530_PRECOMMIT_TASK = dict(V529_PRECOMMIT_TASK)

# v530 attempt 1 verdict (verbatim) — NOT a verbatim copy of the prompt
# example: the audit LLM coined a fresh boundary verdict from the old :15
# rule text. Must be covered independently of the example-echo chain.
V530_PRECOMMIT_COT = {
    "worker_id": 1,
    "cot_consistent": False,
    "discrepancies": [],
    "logical_contradictions": [],
    "boundary_violations": [
        "Architect role introduced and applied the numeric constant "
        "MIN_JAM_COMMITMENT_EQUITY = 0.66, changing a policy threshold "
        "rather than only code structure"
    ],
    "focus_areas": [
        "Confirm whether threshold ownership was explicitly authorized for "
        "this Strategic Regression Repair Architect despite the general "
        "Architect boundary rule",
        "Review the terminal-response jam condition to ensure the 0.66 gate "
        "does not exclude stronger opponent-informed jams affected by the "
        "SPR >= 0.78 alternative"
    ],
}

# v530 attempt 1 diff (verbatim) — adds a named constant and swaps a literal
# for it: identifiers changed, so numbers_only=False.
V530_PRECOMMIT_BEFORE = '''MAX_EQUITY_SAMPLES = 32_768
INITIAL_BATCH_SIZE = 32
MAX_BATCH_SIZE = 512
DEADLINE_GUARD_SECONDS = 0.002
'''
V530_PRECOMMIT_AFTER = '''MAX_EQUITY_SAMPLES = 32_768
INITIAL_BATCH_SIZE = 32
MAX_BATCH_SIZE = 512
MIN_JAM_COMMITMENT_EQUITY = 0.66
DEADLINE_GUARD_SECONDS = 0.002
'''
V530_PRECOMMIT_BEFORE_B = '''    if (
        "allin" in kinds
        and hero_stack > 0
        and safe_equity >= 0.62
        and (spr <= 2.5 or safe_equity >= 0.78)
    ):
'''
V530_PRECOMMIT_AFTER_B = '''    if (
        "allin" in kinds
        and hero_stack > 0
        and safe_equity >= MIN_JAM_COMMITMENT_EQUITY
        and (spr <= 2.5 or safe_equity >= 0.78)
    ):
'''

V529_REVIEW_TASK = {
    "worker_id": "auto_review_repair",
    "role": "Algorithmic Logic Architect",
    "task_kind": "review_repair",
    "repair_blocker": "review_rejection",
    "target_files": ["policy.py"],
    "must_change_files": ["policy.py"],
    "worker_prompt": (
        "This is a Lead Code Reviewer hard-gate repair. Preserve the current "
        "candidate; fix the exact code-quality blocker named by the reviewer."
    ),
}

# v529 attempt 1 verdict (verbatim) — a line-count discrepancy on a repair
# task. The old repair-marker list hard-blocked this ("review_repair").
V529_REVIEW_COT = {
    "worker_id": 1,
    "cot_consistent": False,
    "discrepancies": [
        "Worker claimed a post-worker line count of 1679, while the "
        "authoritative target metadata reports 1674 lines."
    ],
    "logical_contradictions": [],
    "boundary_violations": [],
    "focus_areas": [
        "Confirm the actual post-worker policy.py is 1674 lines and remains "
        "within the applicable limit.",
        "Verify that only _bluff_allowed changed in this repair and that the "
        "retained _polarized_raise_fraction behavior is byte-identical to "
        "the pre-worker candidate."
    ],
}

# v529 attempt 1 diff excerpt (verbatim) — comment rewrite + literal change.
V529_REVIEW_BEFORE = '''        # Air has no showdown fallback after calling a river bet.  Restrict
        # the bounded bluff to a checked-through spot, where a raise wins the
        # pot outright or retains a deterministic subset's fold-equity edge
        # without committing chips against an already-declared bet.
        if to_call > 0.0:
            return False
'''
V529_REVIEW_AFTER = '''        # Preserve the parent pricing boundary for river raises.
        if to_call > 0.50 * pot:
            return False
'''

# v528 worker_1 (innovation task, Algorithmic Logic Architect) — the SIXTH
# recorded inconsistent verdict across the four replayed versions. Under the
# OLD code this verdict did NOT hard-block (no side-effect/mismatch regex
# hit, no repair marker on an innovation task); it must stay advisory under
# the new matrix too (census completeness, F3 of the adversarial audit).
V528_WORKER1_TASK = {
    "worker_id": 1,
    "role": "Algorithmic Logic Architect",
    "task_kind": "strategy_implementation",
    "target_files": ["policy.py"],
    "worker_prompt": (
        "Use exact target bots/national_cloud_v528/policy.py; modify only "
        "the existing function policy.py:_polarized_raise_fraction."
    ),
}
V528_WORKER1_COT = {
    "worker_id": 1,
    "cot_consistent": False,
    "discrepancies": [
        "Worker claimed there were two parent 0.38 polarized returns and "
        "that it routed both through the terminal-response fraction, but "
        "the diff changes only the donk/probe branch; the high-equity "
        "polarized path remains unchanged and still returns fixed values "
        "rather than the evidence-controlled fraction.",
        "Worker claimed the required executable assertions were added, but "
        "the self-test validates only the isolated fraction outputs and "
        "does not exercise the required downstream sizing chain or prove "
        "differing legal raise_to values from fractions 0.38 and 0.46."
    ],
    "logical_contradictions": [
        "The narrative says both 0.38 polarized branches were made "
        "confidence-controlled, while the actual diff defines and consumes "
        "`fraction` only in the donk/probe branch."
    ],
    "boundary_violations": [],
    "focus_areas": [
        "Verify whether proposal a69bc99431c40295 requires both existing "
        "polarized 0.38 branches to consume the evidence gate; if so, "
        "identify why the second branch remains fixed.",
        "Confirm `MAX_ADAPTATION_WEIGHT` equals the contract-required "
        "exclusive-lower/0.65-upper bound and that the helper normalizes "
        "all malformed, boolean, non-finite, and oversized numeric inputs "
        "safely.",
        "Check whether strong versus neutral fractions actually produce "
        "different legal `raise_to` intents through `_raise_intent` and "
        "`_decision_from_equity`, and that clamp-dominated paths are "
        "handled according to the proposal.",
        "Confirm the added `__main__` block complies with runtime "
        "constraints and does not create unintended behavior in production "
        "execution contexts."
    ],
}
V528_WORKER1_BEFORE = '''def _polarized_raise_fraction(context, equity):
    betting = context.get("betting", {}) or {}
    line = context.get("line", {}) or {}
    spr = _bounded(betting.get("spr"), 0.0, 200.0, 20.0)
    to_call = max(0, _integer(betting.get("to_call"), 0))
    if to_call == 0 and (line.get("can_donk") or line.get("can_delayed_probe")):
        if equity >= 0.70:
            return 0.58
        return 0.38 if _bounded(equity, 0.0, 1.0) >= 0.55 else 0.30
    return None
'''
V528_WORKER1_AFTER = '''def _polarized_raise_fraction(context, equity):
    betting = context.get("betting", {}) or {}
    line = context.get("line", {}) or {}
    opponent = context.get("opponent") or {}
    response = opponent.get("terminal_response") or {} if isinstance(opponent, dict) else {}
    raw_fold_rate = response.get("fold_to_raise") if isinstance(response, dict) else None
    raw_weight = response.get("adaptation_weight") if isinstance(response, dict) else None
    fold_rate = _number(raw_fold_rate, -1.0)
    adaptation_weight = _number(raw_weight, -1.0)
    fold_rate_is_valid = (
        isinstance(raw_fold_rate, (int, float))
        and not isinstance(raw_fold_rate, bool)
        and 0.0 <= fold_rate <= 1.0
    )
    weight_is_valid = (
        isinstance(raw_weight, (int, float))
        and not isinstance(raw_weight, bool)
        and 0.0 < adaptation_weight <= MAX_ADAPTATION_WEIGHT
    )
    fraction = 0.38
    if (
        fold_rate_is_valid
        and weight_is_valid
        and fold_rate >= 0.60
        and adaptation_weight >= 0.35
        and fold_rate * adaptation_weight >= 0.22
    ):
        fraction = 0.46
    spr = _bounded(betting.get("spr"), 0.0, 200.0, 20.0)
    to_call = max(0, _integer(betting.get("to_call"), 0))
    if to_call == 0 and (line.get("can_donk") or line.get("can_delayed_probe")):
        if equity >= 0.70:
            return 0.58
        return 0.38 if _bounded(equity, 0.0, 1.0) >= 0.55 else 0.30
    return None
'''


def _diff_facts(pairs):
    """Build per-file diff facts from (before, after) text pairs.

    Mirrors agent_workers._worker_diff_facts: changed/numbers_only come from
    the deterministic numbers-only comparator and added_lines are the '+'
    lines of the unified diff.
    """
    facts = {}
    for rel, (before, after) in pairs.items():
        categories = wrp.classify_diff_change_categories(before, after)
        added = [
            line[1:]
            for line in difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                lineterm="",
            )
            if line.startswith("+") and not line.startswith("+++")
        ]
        facts[rel] = {
            "before_present": True,
            "after_present": True,
            "changed": bool(categories),
            "numbers_only": categories == frozenset({"numeric_constants"}),
            "added_lines": added,
        }
    return facts


V526_FACTS_ATTEMPT3 = _diff_facts(
    {"policy.py": (V526_ARCH_BEFORE_ATTEMPT3, V526_ARCH_AFTER_ATTEMPT3)}
)
V526_FACTS_ATTEMPT4 = _diff_facts(
    {
        "policy.py": (
            V526_ARCH_BEFORE_ATTEMPT4 + V526_ARCH_BEFORE_ATTEMPT4_B,
            V526_ARCH_AFTER_ATTEMPT4 + V526_ARCH_AFTER_ATTEMPT4_B,
        )
    }
)
V529_PRECOMMIT_FACTS = _diff_facts(
    {"policy.py": (V529_PRECOMMIT_BEFORE, V529_PRECOMMIT_AFTER)}
)
V530_PRECOMMIT_FACTS = _diff_facts(
    {
        "policy.py": (
            V530_PRECOMMIT_BEFORE + V530_PRECOMMIT_BEFORE_B,
            V530_PRECOMMIT_AFTER + V530_PRECOMMIT_AFTER_B,
        )
    }
)
V529_REVIEW_FACTS = _diff_facts(
    {"policy.py": (V529_REVIEW_BEFORE, V529_REVIEW_AFTER)}
)
V528_WORKER1_FACTS = _diff_facts(
    {"policy.py": (V528_WORKER1_BEFORE, V528_WORKER1_AFTER)}
)


# ══════════════════════════════════════════════════════════════════════
# (a) False-positive replay — must ALL be advisory now
# ══════════════════════════════════════════════════════════════════════


REPLAY_CASES = [
    (
        "v526_arch_attempt3_opponent_modeler_discrepancy",
        V526_ARCH_TASK,
        V526_ARCH_COT_ATTEMPT3,
        V526_FACTS_ATTEMPT3,
    ),
    (
        "v526_arch_attempt4_opponent_modeler_telemetry_wording",
        V526_ARCH_TASK,
        V526_ARCH_COT_ATTEMPT4,
        V526_FACTS_ATTEMPT4,
    ),
    (
        "v529_precommit_attempt1_architect_numeric_boundary",
        V529_PRECOMMIT_TASK,
        V529_PRECOMMIT_COT,
        V529_PRECOMMIT_FACTS,
    ),
    (
        "v530_precommit_attempt1_architect_coined_boundary",
        V530_PRECOMMIT_TASK,
        V530_PRECOMMIT_COT,
        V530_PRECOMMIT_FACTS,
    ),
    (
        "v529_review_attempt1_line_count_discrepancy",
        V529_REVIEW_TASK,
        V529_REVIEW_COT,
        V529_REVIEW_FACTS,
    ),
]


@pytest.mark.parametrize("label,task,cot,facts", REPLAY_CASES)
def test_replayed_false_positive_chains_no_longer_block(label, task, cot, facts):
    verdict = wrp.review_worker_cot_lead(task, cot, facts)
    assert verdict.block is False, (
        f"replay {label}: LLM CoT verdict must not hard-block without "
        f"deterministic confirmation (downgrade_reasons={verdict.downgrade_reasons})"
    )
    # The lead still surfaces: either it was explicitly downgraded or it
    # was recorded as an advisory note for the Reviewer feedback loop.
    assert verdict.downgrade_reasons or verdict.advisory_notes
    # The recorded diff facts really were structural / non-tuner shapes.
    if cot.get("boundary_violations"):
        assert wrp.normalize_worker_role(task["role"]) != "tuner"


def test_replay_fixture_facts_reflect_recorded_diff_shapes():
    # v526/v529/v530 recorded diffs were structural or numeric+identifier
    # edits by NON-tuner repair roles — the key fact that made the old
    # boundary verdicts false positives.
    for facts in (
        V526_FACTS_ATTEMPT3,
        V526_FACTS_ATTEMPT4,
        V529_PRECOMMIT_FACTS,
        V530_PRECOMMIT_FACTS,
        V529_REVIEW_FACTS,
    ):
        assert facts["policy.py"]["changed"] is True
    # v529 review repair diff contains both a comment rewrite and a numeric
    # change → not numbers-only.
    assert V529_REVIEW_FACTS["policy.py"]["numbers_only"] is False
    assert V530_PRECOMMIT_FACTS["policy.py"]["numbers_only"] is False


# ══════════════════════════════════════════════════════════════════════
# (b) True violations — must STILL block
# ══════════════════════════════════════════════════════════════════════


def test_true_tuner_control_flow_violation_still_blocks():
    task = {
        "worker_id": "w1",
        "role": "Hyperparameter Tuner",
        "task_kind": "feature_work",
        "target_files": ["policy.py"],
    }
    cot = {
        "cot_consistent": False,
        "boundary_violations": [
            "Tuner role but modified an if/else block in policy.py"
        ],
        "discrepancies": [],
        "focus_areas": [],
    }
    facts = _diff_facts(
        {
            "policy.py": (
                "def decide(ctx):\n    if ctx:\n        return 1\n    return 0\n",
                "def decide(ctx):\n    if ctx:\n        return 2\n"
                "    elif ctx.x:\n        return 3\n    return 0\n",
            )
        }
    )
    verdict = wrp.review_worker_cot_lead(task, cot, facts)
    assert verdict.block is True
    # Same judgment as the deterministic _validate_worker_boundaries gate:
    # tuner + non-numbers-only diff.
    assert facts["policy.py"]["numbers_only"] is False


def test_true_runtime_side_effect_still_blocks_and_clean_diff_downgrades():
    task = {
        "worker_id": "w1",
        "role": "Expert Coder 1",
        "task_kind": "feature_work",
        "target_files": ["policy.py"],
    }
    cot = {
        "cot_consistent": False,
        "discrepancies": [
            "Worker added _sys.stderr.write telemetry inside the hot path "
            "but did not disclose this runtime side-effect."
        ],
        "boundary_violations": [],
        "focus_areas": [],
    }
    guilty = _diff_facts(
        {
            "policy.py": (
                "def f(x):\n    return x\n",
                "def f(x):\n    print('trace', x)\n    return x\n",
            )
        }
    )
    clean = _diff_facts(
        {
            "policy.py": (
                "def f(x):\n    return x\n",
                "def f(x):\n    return x + 1\n",
            )
        }
    )
    assert wrp.review_worker_cot_lead(task, cot, guilty).block is True
    verdict = wrp.review_worker_cot_lead(task, cot, clean)
    assert verdict.block is False
    assert verdict.downgrade_reasons


def test_true_task_mismatch_all_targets_unchanged_still_blocks():
    task = {
        "worker_id": "w1",
        "role": "Expert Coder 1",
        "task_kind": "feature_work",
        "target_files": ["policy.py"],
    }
    cot = {
        "cot_consistent": False,
        "discrepancies": [
            "The assigned task steps perform none of these edits; the diff "
            "does not implement the requested change."
        ],
        "boundary_violations": [],
        "focus_areas": [],
    }
    facts = _diff_facts({"policy.py": ("same\n", "same\n")})
    assert facts["policy.py"]["changed"] is False
    assert wrp.review_worker_cot_lead(task, cot, facts).block is True


# ══════════════════════════════════════════════════════════════════════
# Seam contract — agent_workers block/downgrade behavior
# ══════════════════════════════════════════════════════════════════════


class _StubUI:
    def __init__(self):
        self.history = []

    def log_history(self, message, level="info"):
        self.history.append((level, message))

    def clear_io(self):
        return None

    def set_status(self, *_args, **_kwargs):
        return None


@pytest.fixture()
def seam(monkeypatch, tmp_path):
    import agent_workers

    events = []
    resets = []

    monkeypatch.setattr(
        agent_workers, "_reset_target_files_to_source",
        lambda *args, **kwargs: resets.append((args, kwargs)),
    )
    monkeypatch.setattr(
        agent_workers, "_emit_worker_cot_lead_downgraded",
        lambda *args, **kwargs: events.append(("downgraded", args, kwargs)),
    )

    candidate = tmp_path / "national_v526"
    candidate.mkdir()
    # Faithful attempt-4 replay material: the recorded attempt-4 diff
    # (structural edit, NO side-effect token on any added line) against the
    # attempt-4 CoT verdict whose focus_areas merely mention "telemetry".
    (candidate / "policy.py").write_text(
        V526_ARCH_AFTER_ATTEMPT4 + V526_ARCH_AFTER_ATTEMPT4_B,
        encoding="utf-8",
    )
    snapshots = {
        (0, "policy.py"): V526_ARCH_BEFORE_ATTEMPT4 + V526_ARCH_BEFORE_ATTEMPT4_B
    }

    def run(task, cot, skipper=None):
        focus = []
        blocked = agent_workers._handle_worker_cot_inconsistency(
            task, 0, cot, 526, 525, candidate, snapshots,
            _StubUI(), focus, task_skipper=skipper,
        )
        return blocked, focus

    return {
        "run": run,
        "events": events,
        "resets": resets,
        "module": agent_workers,
    }


def test_seam_downgrades_false_positive_without_rollback(seam):
    blocked, focus = seam["run"](V526_ARCH_TASK, V526_ARCH_COT_ATTEMPT4)
    assert blocked is False
    assert seam["resets"] == []
    assert seam["events"], "downgrade event must be emitted"
    # The lead still reaches the Reviewer pipeline.
    assert any("telemetry" in str(item) for item in focus)


def test_seam_blocks_confirmed_lead_with_rollback(seam):
    task = {
        "worker_id": "w1",
        "role": "Hyperparameter Tuner",
        "task_kind": "feature_work",
        "target_files": ["policy.py"],
    }
    cot = {
        "cot_consistent": False,
        "boundary_violations": ["tuner modified control flow"],
        "discrepancies": [],
        "focus_areas": ["check the branch"],
    }
    blocked, focus = seam["run"](task, cot)
    assert blocked is True
    assert len(seam["resets"]) == 1
    assert seam["resets"][0][1].get("task_idx") == 0
    assert any("check the branch" in str(item) for item in focus)


def test_seam_skipper_override_survives_block(seam):
    task = {
        "worker_id": "w1",
        "role": "Hyperparameter Tuner",
        "task_kind": "feature_work",
        "target_files": ["policy.py"],
    }
    cot = {
        "cot_consistent": False,
        "boundary_violations": ["tuner modified control flow"],
        "discrepancies": [],
        "focus_areas": [],
    }
    blocked, _focus = seam["run"](task, cot, skipper=lambda task: "blocker cleared")
    assert blocked is False
    assert seam["resets"] == []


def test_worker_diff_facts_match_audit_diff_material(seam, tmp_path):
    import agent_workers

    candidate = tmp_path / "national_v526"
    candidate.mkdir(exist_ok=True)
    (candidate / "policy.py").write_text(
        V530_PRECOMMIT_AFTER + V530_PRECOMMIT_AFTER_B, encoding="utf-8"
    )
    snapshots = {
        (0, "policy.py"): V530_PRECOMMIT_BEFORE + V530_PRECOMMIT_BEFORE_B
    }
    task = {"target_files": ["policy.py"]}
    facts = agent_workers._worker_diff_facts(
        task, 0, candidate, 526, snapshots
    )
    assert set(facts) == {"policy.py"}
    entry = facts["policy.py"]
    assert entry["changed"] is True
    assert entry["numbers_only"] is False
    assert any(
        "MIN_JAM_COMMITMENT_EQUITY" in line for line in entry["added_lines"]
    )


# ══════════════════════════════════════════════════════════════════════
# Old enforcement surface is gone (the LLM verdict no longer executes)
# ══════════════════════════════════════════════════════════════════════


def test_agent_workers_no_longer_exposes_llm_verdict_enforcement():
    import agent_workers

    for name in (
        "_cot_inconsistency_blocks_task",
        "_cot_inconsistency_has_runtime_side_effect",
        "_cot_inconsistency_is_task_mismatch",
    ):
        assert not hasattr(agent_workers, name), (
            f"agent_workers.{name} must be removed: the LLM CoT verdict "
            "must not own direct block authority"
        )


# ══════════════════════════════════════════════════════════════════════
# F1 (adversarial audit 2026-10-09): comment/identifier mentions of
# telemetry in the diff must NOT confirm a side-effect lead. The cross
# product below is the auditor's exact counterexample material: v526
# attempt3's REAL diff (runtime contract REQUIRED the telemetry comment)
# × v526 attempt4's REAL verdict (focus_areas mention "telemetry").
# ══════════════════════════════════════════════════════════════════════


def test_f1_cross_product_attempt3_diff_attempt4_verdict_no_block():
    verdict = wrp.review_worker_cot_lead(
        V526_ARCH_TASK, V526_ARCH_COT_ATTEMPT4, V526_FACTS_ATTEMPT3
    )
    assert verdict.block is False, (
        "v526 attempt3 diff (telemetry comment required by the runtime "
        "contract) × attempt4 verdict (focus_areas prose mentioning "
        "telemetry) must NOT hard-block: "
        f"{verdict.downgrade_reasons}"
    )
    assert verdict.downgrade_reasons
    # The attempt3 diff genuinely carries a comment mentioning telemetry —
    # the fixture is the real recorded material, not a strawman.
    assert any(
        "telemetry" in line for line in V526_FACTS_ATTEMPT3["policy.py"]["added_lines"]
    )


def test_f1_seam_level_no_rollback_for_comment_telemetry(seam, tmp_path):
    import agent_workers

    candidate = tmp_path / "national_v526b"
    candidate.mkdir()
    (candidate / "policy.py").write_text(
        V526_ARCH_AFTER_ATTEMPT3, encoding="utf-8"
    )
    snapshots = {(0, "policy.py"): V526_ARCH_BEFORE_ATTEMPT3}
    focus = []
    blocked = agent_workers._handle_worker_cot_inconsistency(
        V526_ARCH_TASK, 0, V526_ARCH_COT_ATTEMPT4, 526, 525, candidate,
        snapshots, _StubUI(), focus,
    )
    assert blocked is False
    assert seam["resets"] == []


# ══════════════════════════════════════════════════════════════════════
# F3 (adversarial audit 2026-10-09): the full census of recorded
# inconsistent verdicts across the four replayed versions is SIX (the
# v528 worker_1 innovation verdict below was previously unreported). It
# was advisory under the OLD code and must remain advisory now.
# ══════════════════════════════════════════════════════════════════════


def test_f3_v528_worker1_sixth_recorded_verdict_stays_advisory():
    verdict = wrp.review_worker_cot_lead(
        V528_WORKER1_TASK, V528_WORKER1_COT, V528_WORKER1_FACTS
    )
    assert verdict.block is False
    assert verdict.advisory_notes
    assert V528_WORKER1_FACTS["policy.py"]["changed"] is True


# ══════════════════════════════════════════════════════════════════════
# R2 — prompt surface is rendered from the authority table
# ══════════════════════════════════════════════════════════════════════


def test_worker_cot_prompt_template_has_role_placeholder_and_no_examples():
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1]
        / "core" / "prompts" / "worker_cot_check.md"
    ).read_text(encoding="utf-8")
    assert "{role_boundary_rules}" in template
    for phrase in (
        "Tuner role but modified",
        "should only change constants",
        "increase river bluff frequency",
        "new fold condition was added on line 234",
        "Claimed 'more aggressive'",
    ):
        assert phrase not in template, (
            f"worker_cot_check.md must not carry the verbatim example "
            f"phrase {phrase!r}"
        )
    # The static two-role enumeration is gone; rules render per role.
    assert "Hyperparameter Tuner: should ONLY change" not in template


def test_rendered_cot_prompt_carries_role_rules_for_real_roles():
    from evolution_infra import substitute_template
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1]
        / "core" / "prompts" / "worker_cot_check.md"
    ).read_text(encoding="utf-8")
    for role in (
        "Opponent Modeler",
        "Strategic Regression Repair Architect",
        "Algorithmic Logic Architect",
        "Hyperparameter Tuner",
        "Brand New Unlisted Role",
    ):
        rules = wrp.render_role_boundary_rules(role)
        rendered = substitute_template(
            template,
            {"role_boundary_rules": rules},
        )
        assert "{role_boundary_rules}" not in rendered
        assert rules.strip() in rendered
        for phrase in (
            "Tuner role but modified",
            "should only change constants",
        ):
            assert phrase.lower() not in rendered.lower()


# ══════════════════════════════════════════════════════════════════════
# R3 — v525-shape feedback no longer pins a constants-only Tuner contract
# ══════════════════════════════════════════════════════════════════════


def test_feedback_quality_contracts_v525_shape_yields_architect():
    import tool_planning_quality_repair_targets as repair_targets

    feedback = (
        "1. Reject. In `bots/national_cloud_v525/policy.py`, `_bluff_allowed` "
        "does not implement the selected bounded mechanism. The contract "
        "states not_threshold_tuning: the mechanism adds a bounded continuous "
        "reliability-to-frequency mapping instead of moving a discrete cutoff "
        "or changing fixed thresholds elsewhere; the Hyperparameter Tuner "
        "scope line says EXISTING numeric constants/thresholds/magic numbers "
        "in `policy.py` ONLY, but the required fix rewrites the authorization "
        "branch control flow and adds a new helper function, which no "
        "existing-constant edit can express. The existing constant "
        "RAISE_FLOOR must stay at its parent value."
    )
    contracts = repair_targets._feedback_quality_contracts(feedback)
    assert contracts, "fixture feedback must produce at least one contract"
    policy_contracts = [c for c in contracts if c.get("file") == "policy.py"]
    assert policy_contracts
    for contract in policy_contracts:
        assert contract.get("role_hint") != "tuner", (
            "v525-shape threshold-dense structural feedback must not pin a "
            "constants-only Tuner contract"
        )


def test_feedback_quality_contracts_genuine_boundary_evidence_pins_tuner():
    import tool_planning_quality_repair_targets as repair_targets

    feedback = (
        "1. quality gate failure in `bots/national_cloud_v530/policy.py`: "
        "hyperparameter_boundary_violation — Hyperparameter Tuner changed "
        "non-numeric text or structure; revert the numeric constant "
        "AGGRESSION_BASE to the parent value."
    )
    contracts = repair_targets._feedback_quality_contracts(feedback)
    policy = [c for c in contracts if c.get("file") == "policy.py"]
    assert policy
    assert policy[0].get("role_hint") == "tuner"


def test_quality_contract_task_flips_unsatisfiable_tuner_before_dispatch():
    import tool_planning_quality_contracts as contracts

    task = contracts._quality_contract_task(
        {
            "blocker": "quality_gate",
            "file": "policy.py",
            "evidence": (
                "Reviewer rejection in `policy.py`: the repair requires a "
                "new bounded helper function and rewired control flow; no "
                "constants-only edit can clear the blocker."
            ),
            "role_hint": "tuner",
        },
        {"next_v": 531, "source_v": 530},
        "Preserve the candidate for v{next_v}.",
        "quality_repair",
    )
    assert task["role"] != "Hyperparameter Tuner"
    assert task["role"] == "Algorithmic Logic Architect"
    assert "Constants-only role method" not in task["worker_prompt"]


def test_quality_contract_task_genuine_numeric_contract_keeps_tuner():
    import tool_planning_quality_contracts as contracts

    task = contracts._quality_contract_task(
        {
            "blocker": "quality_gate",
            "file": "policy.py",
            "evidence": (
                "hyperparameter_boundary_violation: revert the numeric "
                "constant AGGRESSION_BASE to the parent value in policy.py"
            ),
            "role_hint": "tuner",
        },
        {"next_v": 531, "source_v": 530},
        "Preserve the candidate for v{next_v}.",
        "quality_repair",
    )
    assert task["role"] == "Hyperparameter Tuner"
    assert "Constants-only role method" in task["worker_prompt"]


def test_rework_dispatch_band_flips_frozen_tuner_tasks():
    """F2 (adversarial audit 2026-10-09): the frozen-resume second line of
    defense must flip at the DISPATCH point, not in the rework band.

    The old wiring flipped ``tasks`` next to ``_task_write_scope_errors``,
    which made Phase C's frozen-input drift comparison
    (``frozen_worker_input.get("tasks") != tasks``) fire and abandon the
    generation — the opposite of the design's "flip instead of refuse".
    The rework band must no longer mutate tasks, and the durable dispatch
    copy must carry the flip.
    """
    import tool_planning_worker_durable as durable
    import tool_planning_worker_phases_rework as rework_phase

    # The band-level mutator is gone: nothing in the rework module may
    # reassign tasks before the frozen-resume drift validation.
    assert not hasattr(rework_phase, "_enforce_repair_contract_satisfiability")

    frozen_task = {
        "worker_id": "auto_quality_repair_gate_policy_py",
        "role": "Hyperparameter Tuner",
        "task_kind": "quality_repair",
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
        "repair_contract": {
            "blocker": "runtime_architecture",
            "evidence": "incremental_opponent_model missing",
        },
    }
    innovation_task = {
        "worker_id": "w2",
        "role": "Hyperparameter Tuner",
        "task_kind": "feature_work",
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
    }
    envelope = {
        "next_v": 531,
        "tasks": [dict(frozen_task), dict(innovation_task)],
    }
    dispatch_tasks = durable._dispatch_tasks_from_envelope(envelope)

    # The dispatch copy flips the unsatisfiable repair contract...
    assert dispatch_tasks[0]["role"] == "Algorithmic Logic Architect"
    # ...and never touches non-repair innovation tasks.
    assert dispatch_tasks[1]["role"] == "Hyperparameter Tuner"
    # The frozen envelope bytes stay identical — the drift comparison and
    # every digest bound at planning time see the ORIGINAL tasks.
    assert envelope["tasks"][0]["role"] == "Hyperparameter Tuner"
    assert envelope["tasks"] == [frozen_task, innovation_task]
    assert dispatch_tasks[0] is not envelope["tasks"][0]
