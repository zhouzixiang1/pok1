"""R2/R3 unit tests for the authoritative worker role policy table.

Companion to ``tests/test_cot_authority_root_fix_20261008.py`` (R1 decision
functions) and ``tests/test_worker_cot_lead_enforcement.py`` (runtime-verdict
replay). This file drives:

* R2 — ``ROLE_CHANGE_CATEGORIES`` authority table, per-role prompt rule
  rendering (five real logged roles + unenumerated roles), digest
  content-binding, and the absence of the old verbatim verdict examples in
  the rendered text and the template file itself;
* R3 — evidence-driven ``classify_repair_role_hint`` (the v525
  threshold-storm shape must NOT pin a constants-only Tuner; genuine
  ``hyperparameter_boundary_violation`` evidence must) and the
  ``repair_contract_satisfiable`` flip matrix.
"""

from pathlib import Path

import worker_role_policy as wrp


# ──────────────────────────────────────────────
# R2 — authoritative role table and prompt rendering
# ──────────────────────────────────────────────


def test_authority_table_tuner_is_strictest_bucket():
    tuner = wrp.allowed_change_categories("Hyperparameter Tuner")
    architect = wrp.allowed_change_categories("Algorithmic Logic Architect")
    other = wrp.allowed_change_categories("Opponent Modeler")
    unknown = wrp.allowed_change_categories("Completely Unknown Role 42")

    assert tuner == frozenset({"numeric_constants"})
    # Non-tuner buckets share the architect set (default-lenient bucket).
    assert architect == frozenset(
        {"numeric_constants", "structure", "imports", "new_code"}
    )
    assert other == architect
    assert unknown == architect
    assert tuner < architect


def test_authority_table_covers_real_logged_roles():
    # Roles observed in the v526-v530 runtime logs.
    for role in (
        "Hyperparameter Tuner",
        "Algorithmic Logic Architect",
        "Opponent Modeler",
        "Strategic Regression Repair Architect",
        "Algorithmic Runtime Architect",
        "Scope Boundary Repair Architect",
    ):
        categories = wrp.allowed_change_categories(role)
        assert categories, f"role {role!r} must resolve to a non-empty category set"
        if wrp.normalize_worker_role(role) == "tuner":
            assert categories == frozenset({"numeric_constants"})


_FORBIDDEN_PROMPT_PHRASES = (
    # The verbatim verdict example from the old worker_cot_check.md:57 — the
    # byte-identical string the v526-v530 audit LLM kept copying.
    "Tuner role but modified",
    "should only change constants",
    # The other example-verbatim strings from the old :55-:58 block.
    "increase river bluff frequency",
    "new fold condition was added on line 234",
    "Claimed 'more aggressive'",
)


def test_render_role_boundary_rules_has_no_verbatim_verdict_examples():
    for role in (
        "Hyperparameter Tuner",
        "Algorithmic Logic Architect",
        "Opponent Modeler",
        "Strategic Regression Repair Architect",
        "Unknown Future Role",
        "",
    ):
        rendered = wrp.render_role_boundary_rules(role)
        for phrase in _FORBIDDEN_PROMPT_PHRASES:
            assert phrase.lower() not in rendered.lower(), (
                f"role {role!r} rendering must not contain the old example "
                f"verdict phrase {phrase!r}"
            )


def test_render_role_boundary_rules_renders_per_role():
    tuner_rules = wrp.render_role_boundary_rules("Hyperparameter Tuner")
    architect_rules = wrp.render_role_boundary_rules(
        "Algorithmic Logic Architect"
    )
    other_rules = wrp.render_role_boundary_rules("Opponent Modeler")
    unknown_rules = wrp.render_role_boundary_rules("Unknown Future Role")

    assert tuner_rules.strip()
    assert architect_rules.strip()
    # Tuner rules are strictly about numeric constants.
    assert "numeric constant" in tuner_rules.lower()
    # The rules name the concrete role so the audit cannot mistake it.
    assert "hyperparameter tuner" in tuner_rules.lower()
    assert "algorithmic logic architect" in architect_rules.lower()
    # Unenumerated roles get an explicit default rule, not silence.
    for rules in (other_rules, unknown_rules):
        assert "deterministic" in rules.lower()
        assert "no prompt-level change-shape restriction" in rules.lower()
    # Every rendering states that the auditor only reports; the system
    # deterministically adjudicates enforcement.
    for rules in (tuner_rules, architect_rules, other_rules, unknown_rules):
        assert "adjudicat" in rules.lower()


def test_role_policy_digest_is_stable_and_content_bound(monkeypatch):
    first = wrp.role_policy_digest()
    assert isinstance(first, str) and len(first) == 64
    assert wrp.role_policy_digest() == first
    # The digest is content-bound to the authoritative table: changing the
    # table must change the digest (audit provenance follows the table).
    monkeypatch.setitem(
        wrp.ROLE_CHANGE_CATEGORIES,
        "tuner",
        frozenset({"numeric_constants", "structure"}),
    )
    assert wrp.role_policy_digest() != first


def test_cot_prompt_template_carries_placeholder_and_no_old_examples():
    template = (
        Path(__file__).resolve().parents[1]
        / "core" / "prompts" / "worker_cot_check.md"
    ).read_text(encoding="utf-8")
    assert "{role_boundary_rules}" in template
    for phrase in _FORBIDDEN_PROMPT_PHRASES:
        assert phrase not in template, (
            f"worker_cot_check.md must not carry the verbatim example "
            f"phrase {phrase!r}"
        )
    # The static two-role enumeration is gone; rules render per role.
    assert "Hyperparameter Tuner: should ONLY change" not in template


def test_rendered_cot_prompt_carries_role_rules_for_real_roles():
    from evolution_infra import substitute_template

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
        for phrase in _FORBIDDEN_PROMPT_PHRASES:
            assert phrase.lower() not in rendered.lower()


# ──────────────────────────────────────────────
# R3 — evidence-driven repair role hint + satisfiability
# ──────────────────────────────────────────────


def _v525_shaped_threshold_dense_feedback():
    """Reviewer-quality feedback with the v525 threshold-storm shape.

    Extracted shape (phrases verbatim from the v525 reviewer log echo): the
    feedback quotes the master-plan Tuner scope line and the contract's
    ``not_threshold_tuning`` language while demanding a structural fix of a
    capability-family blocker. The old five-substring pin read the word
    'threshold'/'hyperparameter tuner' and assigned a constants-only Tuner.
    """
    return (
        "Reject. In `bots/national_cloud_v525/policy.py`, `_bluff_allowed` "
        "does not implement the selected bounded mechanism. The reviewer "
        "contract note says the old pass/fail boundary remains the "
        "zero-frequency point; the mechanism adds a bounded continuous "
        "reliability-to-frequency mapping instead of moving a discrete "
        "cutoff or changing fixed thresholds elsewhere "
        "(not_threshold_tuning). Hyperparameter Tuner scope is EXISTING "
        "numeric constants/thresholds/magic numbers in `policy.py` ONLY; "
        "however the fix must rewire the authorization branch control flow "
        "and add a new bounded helper function, which no threshold-only "
        "edit can express. incremental_opponent_model capability check "
        "still fails: the changed function must consume the opponent "
        "tracker snapshot and prove a sanitized-action counterfactual. "
        "Restore the structural wiring; keep every threshold unchanged."
    )


def test_classify_repair_role_hint_threshold_storm_is_not_tuner():
    feedback = _v525_shaped_threshold_dense_feedback()
    assert feedback.lower().count("threshold") >= 4
    assert wrp.classify_repair_role_hint(feedback) != "tuner"
    assert wrp.classify_repair_role_hint(feedback) == ""


def test_classify_repair_role_hint_genuine_constant_marker_is_tuner():
    assert (
        wrp.classify_repair_role_hint(
            "hyperparameter_boundary_violation: Hyperparameter Tuner changed "
            "non-numeric text or structure in policy.py"
        )
        == "tuner"
    )
    assert (
        wrp.classify_repair_role_hint(
            "Please revert the numeric constant RAISE_FLOOR back to its "
            "parent value in policy.py"
        )
        == "tuner"
    )
    # Plain prose mentioning thresholds/numerics without a deterministic
    # constant-class marker stays unassigned (defaults to Architect).
    assert wrp.classify_repair_role_hint("tighten the call threshold") == ""


def test_repair_contract_satisfiable_flips_structural_tuner_contract():
    decision = wrp.repair_contract_satisfiable({
        "worker_id": "auto_quality_repair_gate_policy_py",
        "role": "Hyperparameter Tuner",
        "task_kind": "quality_repair",
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
        "repair_contract": {
            "blocker": "runtime_architecture",
            "file": "policy.py",
            "evidence": "incremental_opponent_model missing",
        },
    })
    assert decision.satisfiable is False
    assert decision.flip_role == "Algorithmic Logic Architect"
    assert decision.reason


def test_repair_contract_satisfiable_genuine_numeric_contract_stays_tuner():
    task = {
        "worker_id": "auto_quality_repair_gate_policy_py",
        "role": "Hyperparameter Tuner",
        "task_kind": "quality_repair",
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
        "repair_contract": {
            "blocker": "quality_gate",
            "file": "policy.py",
            "evidence": (
                "hyperparameter_boundary_violation: revert the numeric "
                "constant to its parent value"
            ),
        },
    }
    decision = wrp.repair_contract_satisfiable(task)
    assert decision.satisfiable is True
    assert decision.flip_role == ""


def test_repair_contract_satisfiable_multi_file_must_change_flips():
    decision = wrp.repair_contract_satisfiable({
        "role": "Hyperparameter Tuner",
        "task_kind": "quality_repair",
        "target_files": ["policy.py", "national_bot.py"],
        "must_change_files": ["policy.py", "national_bot.py"],
        "repair_contract": {"blocker": "quality_gate", "evidence": "x"},
    })
    assert decision.satisfiable is False
    assert decision.flip_role == "Algorithmic Logic Architect"


def test_repair_contract_satisfiable_structural_evidence_keywords_flip():
    decision = wrp.repair_contract_satisfiable({
        "role": "Hyperparameter Tuner",
        "task_kind": "quality_repair",
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
        "repair_contract": {
            "blocker": "quality_gate",
            "evidence": _v525_shaped_threshold_dense_feedback(),
        },
    })
    assert decision.satisfiable is False
    assert decision.flip_role == "Algorithmic Logic Architect"


def test_repair_contract_satisfiable_non_tuner_roles_are_satisfiable():
    for role in (
        "Algorithmic Logic Architect",
        "Opponent Modeler",
        "Strategic Regression Repair Architect",
    ):
        decision = wrp.repair_contract_satisfiable({
            "role": role,
            "task_kind": "quality_repair",
            "target_files": ["policy.py"],
            "repair_contract": {"blocker": "runtime_architecture"},
        })
        assert decision.satisfiable is True
        assert decision.flip_role == ""


def test_enforce_repair_contract_satisfiability_flips_frozen_tuner_task():
    task = {
        "worker_id": "auto_quality_repair_gate_policy_py",
        "role": "Hyperparameter Tuner",
        "task_kind": "quality_repair",
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
        "worker_prompt": "frozen prompt",
        "repair_contract": {
            "blocker": "precommit_regression",
            "evidence": "regression needs structural correction",
        },
    }
    flipped = wrp.enforce_repair_contract_satisfiability(dict(task))
    assert flipped["role"] == "Algorithmic Logic Architect"
    # Non-repair innovation tasks are never touched by the dispatch-band
    # second line of defense.
    innovation = {
        "worker_id": "w2",
        "role": "Hyperparameter Tuner",
        "task_kind": "feature_work",
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
    }
    assert wrp.enforce_repair_contract_satisfiability(
        dict(innovation)
    )["role"] == "Hyperparameter Tuner"
