"""Root-fix unit tests for the CoT lead/review decision functions (R1).

v525-v530 died six generations in a row on ``quality_rework_circuit_breaker``
because the LLM Worker-CoT audit owned direct block/rollback enforcement.
The root fix moves enforcement into deterministic code: this file drives
the NEW decision functions only — ``classify_diff_change_categories`` and
the ``review_worker_cot_lead`` matrix — with no LLM and no IO.

R2 (role table / prompt rendering) and R3 (repair-role classifier /
satisfiability) unit tests live in ``tests/test_worker_role_policy.py``;
the runtime-verdict replay lives in
``tests/test_worker_cot_lead_enforcement.py``.
"""

import worker_role_policy as wrp


# ──────────────────────────────────────────────
# classify_diff_change_categories
# ──────────────────────────────────────────────


def test_classify_diff_numbers_only_change():
    before = "THRESH = 0.20\nif x > THRESH:\n    return 1\n"
    after = "THRESH = 0.28\nif x > THRESH:\n    return 1\n"
    categories = wrp.classify_diff_change_categories(before, after)
    assert categories == frozenset({"numeric_constants"})


def test_classify_diff_structural_change():
    before = "def f(x):\n    return 1\n"
    after = "def f(x):\n    if x:\n        return 1\n    return 0\n"
    categories = wrp.classify_diff_change_categories(before, after)
    assert "structure" in categories
    assert "numeric_constants" not in categories


def test_classify_diff_runtime_side_effect_on_added_line():
    before = "def f(x):\n    return x + 1\n"
    after = "def f(x):\n    print('hit f')\n    return x + 1\n"
    categories = wrp.classify_diff_change_categories(before, after)
    assert "runtime_side_effect" in categories


def test_classify_diff_comment_telemetry_is_not_a_side_effect():
    # F1 (adversarial audit 2026-10-09): the diff confirmation layer must
    # match real side-effect CODE, not prose mentions. v526's runtime-
    # architecture repair contract explicitly requires telemetry prose
    # ('prove a typed-intent counterfactual plus telemetry'), so comment /
    # identifier mentions of telemetry/debug must NOT confirm a lead.
    before = "a = 1\n"
    after = "a = 1\n# Reducer-owned action_profile telemetry: bounded.\n"
    categories = wrp.classify_diff_change_categories(before, after)
    assert "runtime_side_effect" not in categories
    assert "structure" in categories  # the comment addition is structural


def test_classify_diff_side_effect_requires_real_code_call():
    before = "def f(x):\n    return x + 1\n"
    for guilty_added_line in (
        "    print('trace', x)",              # real output call
        "    logging.info('debug path')",      # real logging call
        "    sys.stderr.write('note')",        # real stderr write
        "    _sys.stderr.write('note')",       # private-alias stderr write
        "    sys.stdout.write('banner')",      # real stdout write
        "    value = sys.stderr",              # stderr touched in code
    ):
        after = "def f(x):\n" + guilty_added_line + "\n    return x + 1\n"
        categories = wrp.classify_diff_change_categories(before, after)
        assert "runtime_side_effect" in categories, guilty_added_line
    for innocent_added_line in (
        "    # telemetry: reducer-owned, bounded",     # comment mention
        "    action_signal = _opponent_context_profile(",  # identifier prose
        "    x = 1  # debug note",                      # trailing comment
        '    reason = "telemetry only"',                # string literal prose
    ):
        after = "def f(x):\n" + innocent_added_line + "\n    return x + 1\n"
        categories = wrp.classify_diff_change_categories(before, after)
        assert "runtime_side_effect" not in categories, innocent_added_line


def test_classify_diff_unchanged_yields_empty():
    before = "a = 1\n"
    assert wrp.classify_diff_change_categories(before, before) == frozenset()


def test_classify_diff_new_code_only_addition():
    before = ""
    after = "def helper():\n    return 2\n"
    categories = wrp.classify_diff_change_categories(before, after)
    assert "structure" in categories


# ──────────────────────────────────────────────
# R1 — review_worker_cot_lead decision matrix
# ──────────────────────────────────────────────


def _facts(changed=True, numbers_only=False, added_lines=(), rel="policy.py"):
    return {
        rel: {
            "before_present": True,
            "after_present": True,
            "changed": changed,
            "numbers_only": numbers_only,
            "added_lines": list(added_lines),
        }
    }


def test_boundary_lead_blocks_only_for_real_tuner_with_structural_diff():
    task = {
        "worker_id": "t1",
        "role": "Hyperparameter Tuner",
        "task_kind": "feature_work",
        "target_files": ["policy.py"],
    }
    cot = {
        "cot_consistent": False,
        "boundary_violations": ["tuner-shaped boundary violation"],
        "discrepancies": [],
        "focus_areas": [],
    }
    structural = _facts(numbers_only=False)
    numeric = _facts(numbers_only=True)

    verdict = wrp.review_worker_cot_lead(task, cot, structural)
    assert verdict.block is True
    assert "tuner" in verdict.reason and "non" in verdict.reason

    # Same lead, but the diff is numbers-only → the deterministic tuner
    # boundary did NOT fire → downgrade (the quality gate owns that check).
    verdict = wrp.review_worker_cot_lead(task, cot, numeric)
    assert verdict.block is False
    assert verdict.downgrade_reasons


def test_boundary_lead_downgraded_for_non_tuner_roles():
    # v530 shape: 'Strategic Regression Repair Architect' boundary verdict.
    task = {
        "worker_id": "auto_precommit_repair_policy_py",
        "role": "Strategic Regression Repair Architect",
        "task_kind": "precommit_repair",
        "target_files": ["policy.py"],
    }
    cot = {
        "cot_consistent": False,
        "boundary_violations": [
            "Architect role introduced and applied the numeric constant "
            "MIN_JAM_COMMITMENT_EQUITY = 0.66, changing a policy threshold "
            "rather than only code structure"
        ],
        "discrepancies": [],
        "focus_areas": [],
    }
    verdict = wrp.review_worker_cot_lead(task, cot, _facts(numbers_only=False))
    assert verdict.block is False
    assert verdict.downgrade_reasons
    assert verdict.advisory_notes


def test_runtime_side_effect_lead_requires_diff_confirmation():
    task = {
        "worker_id": "w1",
        "role": "Expert Coder 1",
        "task_kind": "feature_work",
        "target_files": ["policy.py"],
    }
    cot_side_effect_claim = {
        "cot_consistent": False,
        "discrepancies": [
            "Worker added _sys.stderr.write telemetry inside the hot path "
            "but did not disclose this runtime side-effect."
        ],
        "boundary_violations": [],
        "focus_areas": [],
    }
    with_print = _facts(added_lines=["    print('hit')"])
    without = _facts(added_lines=["    x = 1"])

    assert wrp.review_worker_cot_lead(
        task, cot_side_effect_claim, with_print
    ).block is True
    downgraded = wrp.review_worker_cot_lead(task, cot_side_effect_claim, without)
    assert downgraded.block is False
    assert downgraded.downgrade_reasons


def test_task_mismatch_lead_blocks_only_when_all_targets_unchanged():
    task = {
        "worker_id": "w1",
        "role": "Expert Coder 1",
        "task_kind": "quality_repair",
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
    all_unchanged = _facts(changed=False)
    some_changed = _facts(changed=True, numbers_only=False)

    assert wrp.review_worker_cot_lead(task, cot, all_unchanged).block is True
    verdict = wrp.review_worker_cot_lead(task, cot, some_changed)
    assert verdict.block is False
    assert verdict.downgrade_reasons


def test_task_mismatch_lead_not_confirmed_when_facts_do_not_cover_targets():
    task = {
        "worker_id": "w1",
        "role": "Expert Coder 1",
        "task_kind": "quality_repair",
        "target_files": ["policy.py", "helper.py"],
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
    # Facts cover only policy.py — helper.py is unproven, so "all targets
    # unchanged" cannot be confirmed; must-change/boundary gates stay the
    # authority.
    partial = _facts(changed=False)
    verdict = wrp.review_worker_cot_lead(task, cot, partial)
    assert verdict.block is False
    assert verdict.downgrade_reasons


def test_repair_discrepancies_are_always_advisory():
    # v526 shape: repair task with a claim-vs-diff discrepancy (no boundary,
    # no side-effect, no provable mismatch). The old code hard-blocked every
    # repair marker; the new contract makes plain discrepancies advisory.
    task = {
        "worker_id": "auto_runtime_architecture_policy_py",
        "role": "Opponent Modeler",
        "task_kind": "quality_repair",
        "target_files": ["policy.py"],
    }
    cot = {
        "cot_consistent": False,
        "discrepancies": [
            "Worker metadata claims `_opponent_context_profile` was changed, "
            "but the actual +25-line diff only modifies `get_baseline_decision`."
        ],
        "boundary_violations": [],
        "focus_areas": ["Confirm get_baseline_decision is the only modified function."],
    }
    verdict = wrp.review_worker_cot_lead(task, cot, _facts(numbers_only=False))
    assert verdict.block is False
    assert verdict.advisory_notes


def test_missing_diff_facts_never_block():
    task = {
        "worker_id": "w1",
        "role": "Expert Coder 1",
        "task_kind": "precommit_repair",
        "target_files": ["policy.py"],
    }
    cot = {
        "cot_consistent": False,
        "boundary_violations": ["anything"],
        "discrepancies": ["undisclosed stderr telemetry side effect"],
        "focus_areas": [],
    }
    verdict = wrp.review_worker_cot_lead(task, cot, None)
    assert verdict.block is False
    assert verdict.downgrade_reasons


def test_consistent_cot_never_blocks():
    task = {
        "worker_id": "w1",
        "role": "Hyperparameter Tuner",
        "task_kind": "feature_work",
        "target_files": ["policy.py"],
    }
    verdict = wrp.review_worker_cot_lead(
        task,
        {"cot_consistent": True, "boundary_violations": []},
        _facts(numbers_only=False),
    )
    assert verdict.block is False


def test_verdict_is_serializable():
    verdict = wrp.review_worker_cot_lead(
        {"role": "Hyperparameter Tuner", "target_files": ["policy.py"]},
        {"cot_consistent": False, "boundary_violations": ["x"]},
        _facts(numbers_only=False),
    )
    payload = verdict.to_dict()
    assert isinstance(payload, dict)
    assert payload["block"] is True
    assert payload["reason"]
