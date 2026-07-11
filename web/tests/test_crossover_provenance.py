from crossover_provenance import (
    python_source_snapshot,
    validate_crossover_recombination_provenance,
)


def _roots(tmp_path):
    parent_a = tmp_path / "national_v1"
    parent_b = tmp_path / "national_v2"
    child = tmp_path / "national_v3"
    for root in (parent_a, parent_b, child):
        root.mkdir()
    return parent_a, parent_b, child


def test_threshold_only_crossover_mutation_is_rejected(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "DEFEND_THRESHOLD = 0.44\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "DEFEND_THRESHOLD = 0.52\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "DEFEND_THRESHOLD = 0.40\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_numeric_mutation_not_in_parent_b"
        for item in issues
    )


def test_exact_parent_b_module_is_accepted(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text("STYLE = 'A'\n", encoding="utf-8")
    (parent_b / "strategy.py").write_text("STYLE = 'B'\n", encoding="utf-8")
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text("STYLE = 'B'\n", encoding="utf-8")

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []


def test_composed_parent_b_component_allows_nonnumeric_glue(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return fallback(state)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def imported_component(state):\n    return profile_action(state)\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def imported_component(state):\n"
        "    return profile_action(state)\n\n"
        "def decide(state):\n"
        "    action = imported_component(state)\n"
        "    return action\n",
        encoding="utf-8",
    )

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []


def test_noop_or_comment_only_child_is_valid_baseline(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text("VALUE = True\n", encoding="utf-8")
    (parent_b / "strategy.py").write_text("OTHER = True\n", encoding="utf-8")
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "# recombination found no safe component\nVALUE = True\n",
        encoding="utf-8",
    )

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []


def test_crossover_cannot_replace_system_owned_native_runtime(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "national_bot.py").write_text("RUNTIME = 'A'\n", encoding="utf-8")
    (parent_b / "national_bot.py").write_text("RUNTIME = 'B'\n", encoding="utf-8")
    baseline = python_source_snapshot(parent_a)
    (child / "national_bot.py").write_text("RUNTIME = 'B'\n", encoding="utf-8")

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert issues == [{
        "path": "national_bot.py",
        "reason": "system_owned_runtime_changed_during_crossover",
    }]


def test_parent_b_import_cannot_camouflage_novel_branch_logic(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return fallback(state)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "from opponent import imported_profile\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "from opponent import imported_profile\n\n"
        "def decide(state):\n"
        "    if state.is_scared:\n"
        "        return fold_action(state)\n"
        "    return fallback(state)\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "if state.is_scared:" in item["lines"]
        for item in issues
    )


def test_parent_b_import_cannot_authorize_unrelated_call_glue(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return fallback(state)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "from opponent import imported_profile\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "from opponent import imported_profile\n\n"
        "def decide(state):\n"
        "    return allin(state)\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "return allin(state)" in item["lines"]
        for item in issues
    )


def test_parent_b_symbol_shadowed_by_parent_a_local_is_not_valid_glue(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n"
        "    imported_component = allin\n"
        "    return fallback(state)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def imported_component(state):\n    return profile_action(state)\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def imported_component(state):\n"
        "    return profile_action(state)\n\n"
        "def decide(state):\n"
        "    imported_component = allin\n"
        "    return imported_component(state)\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "return imported_component(state)" in item["lines"]
        for item in issues
    )


def test_parent_b_call_result_local_remains_valid_glue(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return fallback(state)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def imported_component(state):\n    return profile_action(state)\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def imported_component(state):\n"
        "    return profile_action(state)\n\n"
        "def decide(state):\n"
        "    action = imported_component(state)\n"
        "    return action\n",
        encoding="utf-8",
    )

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []


def test_parent_b_call_result_does_not_authorize_novel_attribute(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(ctx):\n    return fallback(ctx)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def build(ctx):\n    return ctx\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def build(ctx):\n"
        "    return ctx\n\n"
        "def decide(ctx):\n"
        "    selected = build(ctx)\n"
        "    return selected.novel_policy\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "return selected.novel_policy" in item["lines"]
        for item in issues
    )


def test_parent_b_call_result_does_not_authorize_novel_method(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(ctx):\n    return fallback(ctx)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def build(ctx):\n    return ctx\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def build(ctx):\n"
        "    return ctx\n\n"
        "def decide(ctx):\n"
        "    selected = build(ctx)\n"
        "    selected.clear()\n"
        "    return selected\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "selected.clear()" in item["lines"]
        for item in issues
    )


def test_parent_a_context_novel_attribute_is_not_valid_b_call_argument(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(ctx):\n    return ctx\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def build(value):\n    return value\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def build(value):\n"
        "    return value\n\n"
        "def decide(ctx):\n"
        "    return build(ctx.novel_policy)\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "return build(ctx.novel_policy)" in item["lines"]
        for item in issues
    )


def test_parent_a_local_novel_attribute_is_not_valid_b_call_argument(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(ctx):\n"
        "    selected = ctx.profile\n"
        "    return fallback(selected)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def build(value):\n    return value\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def build(value):\n"
        "    return value\n\n"
        "def decide(ctx):\n"
        "    selected = ctx.profile\n"
        "    return build(selected.novel_policy)\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "return build(selected.novel_policy)" in item["lines"]
        for item in issues
    )


def test_parent_a_observed_context_attribute_is_valid_b_call_argument(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(ctx):\n    return fallback(ctx.known_policy)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def build(value):\n    return value\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def build(value):\n"
        "    return value\n\n"
        "def decide(ctx):\n"
        "    return build(ctx.known_policy)\n",
        encoding="utf-8",
    )

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []


def test_novel_non_python_artifact_is_rejected(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text("STYLE = 'A'\n", encoding="utf-8")
    (parent_b / "strategy.py").write_text("STYLE = 'B'\n", encoding="utf-8")
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text("STYLE = 'A'\n", encoding="utf-8")
    (child / "policy_weights.json").write_bytes(b'{"jam": 0.91}\n')

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item == {
            "path": "policy_weights.json",
            "reason": "new_non_python_artifact_not_exact_parent_b",
        }
        for item in issues
    )


def test_replaced_non_python_artifact_must_be_exact_parent_b_bytes(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "policy_weights.bin").write_bytes(b"parent-a-weights")
    (parent_b / "policy_weights.bin").write_bytes(b"parent-b-weights")
    baseline = python_source_snapshot(parent_a)
    (child / "policy_weights.bin").write_bytes(b"independent-weights")

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item == {
            "path": "policy_weights.bin",
            "reason": "non_python_artifact_not_exact_parent_b",
        }
        for item in issues
    )


def test_exact_parent_b_non_python_artifact_is_accepted(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "policy_weights.bin").write_bytes(b"parent-a-weights")
    (parent_b / "policy_weights.bin").write_bytes(b"parent-b-weights\x00\xff")
    baseline = python_source_snapshot(parent_a)
    (child / "policy_weights.bin").write_bytes(b"parent-b-weights\x00\xff")

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []


def test_non_python_deletion_must_match_parent_b_absence(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "policy_weights.json").write_text("{}\n", encoding="utf-8")
    (parent_b / "policy_weights.json").write_text("{\"b\": true}\n", encoding="utf-8")
    baseline = python_source_snapshot(parent_a)

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item == {
            "path": "policy_weights.json",
            "reason": "deleted_file_not_traceable_to_parent_b",
        }
        for item in issues
    )


def test_non_python_deletion_is_traceable_when_parent_b_omits_artifact(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "policy_weights.json").write_text("{}\n", encoding="utf-8")
    baseline = python_source_snapshot(parent_a)

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []


def test_complete_manifest_rejects_novel_empty_directory(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    baseline = python_source_snapshot(parent_a)
    (child / "model_assets").mkdir()

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert {
        "path": "model_assets",
        "reason": "directory_mutation_not_traceable_to_parent_b",
    } in issues


def test_complete_manifest_accepts_exact_parent_b_empty_directory(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_b / "model_assets").mkdir()
    baseline = python_source_snapshot(parent_a)
    (child / "model_assets").mkdir()

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []


def test_unrelated_parent_b_return_line_is_not_component_evidence(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return safe_action(state)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def fold_on_parse_error(error):\n    return -1\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def decide(state):\n    return -1\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_numeric_mutation_not_in_parent_b"
        for item in issues
    )
    assert any(
        item["reason"] == "no_parent_b_component_evidence"
        for item in issues
    )


def test_parent_b_marker_cannot_explain_deleting_parent_a_strategy(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return safe_action(state)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "B_MARKER = True\n\n"
        "def parent_b_component(state):\n"
        "    return state\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text("B_MARKER = True\n", encoding="utf-8")

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"]
        == "parent_a_component_deleted_without_parent_b_or_glue_replacement"
        and ("function", "decide") in map(tuple, item["component"])
        for item in issues
    )


def test_parent_b_marker_cannot_be_used_as_replacement_decision(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return safe_action(state)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "B_MARKER = True\n\n"
        "def parent_b_component(state):\n"
        "    return state\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "B_MARKER = True\n\n"
        "def decide(state):\n"
        "    return B_MARKER\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "return B_MARKER" in item["lines"]
        for item in issues
    )


def test_unused_parent_b_module_import_cannot_authorize_new_attribute_call(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return safe_action(state)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text("import random\n", encoding="utf-8")
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "import random\n\n"
        "def decide(state):\n"
        "    return random.choice(state)\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "return random.choice(state)" in item["lines"]
        for item in issues
    )


def test_parent_b_module_attribute_call_is_valid_only_when_b_used_same_chain(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return safe_action(state)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "import random\n\n"
        "def parent_b_probe(options):\n"
        "    return random.choice(options)\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "import random\n\n"
        "def decide(state):\n"
        "    return random.choice(state)\n",
        encoding="utf-8",
    )

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []


def test_explicit_parent_b_from_import_is_valid_direct_call_glue(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return safe_action(state)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "from profile import imported_component\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "from profile import imported_component\n\n"
        "def decide(state):\n"
        "    action = imported_component(state)\n"
        "    return action\n",
        encoding="utf-8",
    )

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []


def test_parent_a_side_effect_component_cannot_be_duplicated(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "STATE = []\n"
        "STATE.append('a')\n\n"
        "def decide(state):\n"
        "    return state\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "B_MARKER = True\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "STATE = []\n"
        "STATE.append('a')\n"
        "STATE.append('a')\n\n"
        "def decide(state):\n"
        "    return state\n\n"
        "B_MARKER = True\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "parent_component_multiplicity_exceeded"
        and "STATE.append('a')" in item["lines"]
        for item in issues
    )


def test_parent_a_delete_component_cannot_be_duplicated(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "CACHE = {}\n"
        "del CACHE['stale']\n\n"
        "def decide(state):\n"
        "    return state\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text("B_MARKER = True\n", encoding="utf-8")
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "CACHE = {}\n"
        "del CACHE['stale']\n"
        "del CACHE['stale']\n\n"
        "def decide(state):\n"
        "    return state\n\n"
        "B_MARKER = True\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "parent_component_multiplicity_exceeded"
        and "del CACHE['stale']" in item["lines"]
        for item in issues
    )


def test_parent_b_function_component_cannot_be_reused(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return state\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def imported_component(state):\n    return state\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def decide(state):\n"
        "    return state\n\n"
        "def imported_component(state):\n"
        "    return state\n\n"
        "def imported_component(state):\n"
        "    return state\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "parent_component_multiplicity_exceeded"
        and "def imported_component(state):" in item["lines"]
        for item in issues
    )


def test_parent_b_import_component_cannot_be_reused(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return state\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "from profile import imported_component\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "from profile import imported_component\n"
        "from profile import imported_component\n\n"
        "def decide(state):\n"
        "    return state\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "parent_component_multiplicity_exceeded"
        and "from profile import imported_component" in item["lines"]
        for item in issues
    )


def test_parent_a_raw_and_normalized_forms_share_one_component_budget(
    tmp_path,
    monkeypatch,
):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "LIMIT = 1\n\n"
        "def decide(state):\n"
        "    return state\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text("B_MARKER = True\n", encoding="utf-8")
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "LIMIT = 1\n"
        "LIMIT = 2\n\n"
        "def decide(state):\n"
        "    return state\n\n"
        "B_MARKER = True\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "crossover_provenance._apply_system_normalizations",
        lambda _path, source: source.replace("LIMIT = 1", "LIMIT = 2"),
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "parent_component_multiplicity_exceeded"
        and any(line in {"LIMIT = 1", "LIMIT = 2"} for line in item["lines"])
        for item in issues
    )


def test_parent_b_raw_and_normalized_forms_share_one_component_budget(
    tmp_path,
    monkeypatch,
):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(state):\n    return state\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text("LIMIT = 1\n", encoding="utf-8")
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "LIMIT = 1\n"
        "LIMIT = 2\n\n"
        "def decide(state):\n"
        "    return state\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "crossover_provenance._apply_system_normalizations",
        lambda _path, source: source.replace("LIMIT = 1", "LIMIT = 2"),
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "parent_component_multiplicity_exceeded"
        and any(line in {"LIMIT = 1", "LIMIT = 2"} for line in item["lines"])
        for item in issues
    )


def test_single_parent_a_normalized_component_consumes_its_raw_slot(
    tmp_path,
    monkeypatch,
):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "LIMIT = 1\n\n"
        "def decide(state):\n"
        "    return state\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text("B_MARKER = True\n", encoding="utf-8")
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "LIMIT = 2\n\n"
        "def decide(state):\n"
        "    return state\n\n"
        "B_MARKER = True\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "crossover_provenance._apply_system_normalizations",
        lambda _path, source: source.replace("LIMIT = 1", "LIMIT = 2"),
    )

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []


def test_same_parent_b_callable_cannot_be_repeated_in_glue(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(ctx):\n    return fallback(ctx)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def sample(ctx):\n    return ctx\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def sample(ctx):\n"
        "    return ctx\n\n"
        "def decide(ctx):\n"
        "    first = sample(ctx)\n"
        "    second = sample(ctx)\n"
        "    return second\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "second = sample(ctx)" in item["lines"]
        for item in issues
    )


def test_repeated_bare_parent_b_calls_cannot_create_stochastic_policy(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(ctx):\n    return fallback(ctx)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def sample(ctx):\n    return ctx.draw()\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def sample(ctx):\n"
        "    return ctx.draw()\n\n"
        "def decide(ctx):\n"
        "    sample(ctx)\n"
        "    sample(ctx)\n"
        "    return sample(ctx)\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and item["lines"].count("sample(ctx)") == 2
        and "return sample(ctx)" in item["lines"]
        for item in issues
    )


def test_bare_parent_b_call_expression_is_not_valid_glue(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(ctx):\n    return fallback(ctx)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def sample(ctx):\n"
        "    return ctx\n\n"
        "def select(ctx):\n"
        "    return ctx\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def sample(ctx):\n"
        "    return ctx\n\n"
        "def select(ctx):\n"
        "    return ctx\n\n"
        "def decide(ctx):\n"
        "    sample(ctx)\n"
        "    return select(ctx)\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "sample(ctx)" in item["lines"]
        for item in issues
    )


def test_parent_b_call_result_must_be_consumed_by_single_dataflow(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(ctx):\n    return fallback(ctx)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def sample(ctx):\n"
        "    return ctx\n\n"
        "def select(ctx):\n"
        "    return ctx\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def sample(ctx):\n"
        "    return ctx\n\n"
        "def select(ctx):\n"
        "    return ctx\n\n"
        "def decide(ctx):\n"
        "    ignored = sample(ctx)\n"
        "    return select(ctx)\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "ignored = sample(ctx)" in item["lines"]
        for item in issues
    )


def test_parent_b_call_result_cannot_fan_out_in_glue(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(ctx):\n    return fallback(ctx)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def sample(ctx):\n"
        "    return ctx\n\n"
        "def select(left, right):\n"
        "    return left\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def sample(ctx):\n"
        "    return ctx\n\n"
        "def select(left, right):\n"
        "    return left\n\n"
        "def decide(ctx):\n"
        "    sampled = sample(ctx)\n"
        "    return select(sampled, sampled)\n",
        encoding="utf-8",
    )

    issues = validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    )

    assert any(
        item["reason"] == "independent_logic_not_parent_b_component"
        and "return select(sampled, sampled)" in item["lines"]
        for item in issues
    )


def test_distinct_parent_b_calls_can_form_single_linear_glue_chain(tmp_path):
    parent_a, parent_b, child = _roots(tmp_path)
    (parent_a / "strategy.py").write_text(
        "def decide(ctx):\n    return fallback(ctx)\n",
        encoding="utf-8",
    )
    (parent_b / "strategy.py").write_text(
        "def sample(ctx):\n"
        "    return ctx\n\n"
        "def select(value):\n"
        "    return value\n",
        encoding="utf-8",
    )
    baseline = python_source_snapshot(parent_a)
    (child / "strategy.py").write_text(
        "def sample(ctx):\n"
        "    return ctx\n\n"
        "def select(value):\n"
        "    return value\n\n"
        "def decide(ctx):\n"
        "    sampled = sample(ctx)\n"
        "    selected = select(sampled)\n"
        "    return selected\n",
        encoding="utf-8",
    )

    assert validate_crossover_recombination_provenance(
        baseline,
        parent_b,
        child,
    ) == []
