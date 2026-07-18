from pathlib import Path

import pytest

from national_capability_contract import evaluate_national_capabilities
from national_native import NATIVE_BOT_TEMPLATE, NATIVE_PRECOMPUTE_TEMPLATE
from tool_gates import (
    _national_acceptance_executed,
    _national_acceptance_not_run,
)


BOOTSTRAP_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "bootstrap_assets"
    / "strict_v1"
    / "policy.py"
)


POLICY_FOOTER = '''\

def iter_decisions(context, baseline, deadline):
    if context["deadline"] and deadline < 0:
        yield baseline
    return
'''


def _write_bot(root: Path, policy: str) -> Path:
    root.mkdir()
    (root / "national_bot.py").write_text(
        NATIVE_BOT_TEMPLATE,
        encoding="utf-8",
    )
    (root / "precompute.py").write_text(
        NATIVE_PRECOMPUTE_TEMPLATE,
        encoding="utf-8",
    )
    (root / "policy.py").write_text(policy, encoding="utf-8")
    (root / "national_runtime_manifest.json").write_text("{}\n", encoding="utf-8")
    (root / "policy_epoch_receipt.json").write_text("{}\n", encoding="utf-8")
    return root


def _check(result: dict, check_id: str) -> dict:
    return result["checks_by_id"][check_id]


@pytest.mark.parametrize(
    ("prefix", "body", "action"),
    [
        ("", '    return "check"\n', "check"),
        ("", '    return "fold"\n', "fold"),
        ('WIRE_ACTION = "call"\n', "    return WIRE_ACTION\n", "call"),
        (
            "",
            '    wire_action = "allin"\n'
            "    alias = wire_action\n"
            "    return alias\n",
            "allin",
        ),
        ("", '    return "raise 400"\n', "raise 400"),
    ],
)
def test_typed_intent_rejects_bare_action_return_and_simple_alias(
    tmp_path,
    prefix,
    body,
    action,
):
    policy = (
        prefix
        + "def get_baseline_decision(context):\n"
        + "    legal = context['legal']\n"
        + "    betting = context['betting']\n"
        + "    opponent = context['opponent']\n"
        + body
        + POLICY_FOOTER
    )

    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))
    typed = _check(result, "typed_intent_v1")

    assert typed["passed"] is False
    assert typed["evidence"]["forbidden_kind_literals"] == []
    assert any(
        location.endswith(f":bare_action_return:{action}")
        for location in typed["evidence"]["bare_action_return_locations"]
    )


def test_typed_intent_rejects_conditional_bare_action_return_paths(tmp_path):
    policy = '''\
def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    if context["hand"].get("street") == "turn":
        branch_action = "check"
    else:
        branch_action = "fold"
    if context["line"].get("opponent_action"):
        return branch_action
    return "check" if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER

    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))
    typed = _check(result, "typed_intent_v1")

    assert typed["passed"] is False
    locations = typed["evidence"]["bare_action_return_locations"]
    assert any(location.endswith(":bare_action_return:check") for location in locations)
    assert any(location.endswith(":bare_action_return:fold") for location in locations)


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (
            '''\
def _wire_helper():
    return "check"

def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return _wire_helper()
'''
            + POLICY_FOOTER,
            "bare_action_return:check",
        ),
        (
            '''\
def _passthrough(value="fold"):
    return value

def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return _passthrough("fold")
'''
            + POLICY_FOOTER,
            "bare_action_return:fold",
        ),
        (
            '''\
def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return "{}".format("allin")
'''
            + POLICY_FOOTER,
            "bare_action_return:allin",
        ),
        (
            '''\
ACTION_CHOICES = ("raise 400",)

def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return ACTION_CHOICES[0]
'''
            + POLICY_FOOTER,
            "bare_action_return:raise 400",
        ),
        (
            '''\
def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    action = "c"
    action += "all"
    return action
'''
            + POLICY_FOOTER,
            "bare_action_return:call",
        ),
    ],
)
def test_typed_intent_rejects_bounded_obfuscated_bare_action_outputs(
    tmp_path,
    policy,
    expected,
):
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))
    typed = _check(result, "typed_intent_v1")

    assert typed["passed"] is False
    assert any(
        location.endswith(expected)
        for location in typed["evidence"]["bare_action_return_locations"]
    )


def test_typed_intent_rejects_helper_yield_and_unresolved_helper_cycle(tmp_path):
    helper_yield = '''\
def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "pass"}

def _wire_generator():
    yield "allin"

def iter_decisions(context, baseline, deadline):
    yield from _wire_generator()
'''
    yield_result = evaluate_national_capabilities(
        _write_bot(tmp_path / "yield", helper_yield)
    )
    yield_typed = _check(yield_result, "typed_intent_v1")
    assert yield_typed["passed"] is False
    assert any(
        location.endswith("bare_action_yield:allin")
        for location in yield_typed["evidence"]["bare_action_return_locations"]
    )

    cyclic = '''\
def _cycle():
    return _cycle()

def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return _cycle()
'''
    cycle_result = evaluate_national_capabilities(
        _write_bot(tmp_path / "cycle", cyclic + POLICY_FOOTER)
    )
    cycle_typed = _check(cycle_result, "typed_intent_v1")
    assert cycle_typed["passed"] is False
    assert any(
        location.endswith("bare_action_return:unresolved_helper")
        for location in cycle_typed["evidence"]["bare_action_return_locations"]
    )


def test_typed_intent_allows_typed_helper_output(tmp_path):
    policy = '''\
def _typed_helper():
    return {"kind": "pass"}

def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return _typed_helper()
''' + POLICY_FOOTER

    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))
    typed = _check(result, "typed_intent_v1")

    assert typed["passed"] is True
    assert typed["evidence"]["bare_action_return_locations"] == []


@pytest.mark.parametrize(
    ("prefix", "body", "action"),
    [
        ("", '    yield "check"\n', "check"),
        ("", '    return (yield "fold")\n', "fold"),
        (
            'REFINEMENT_ACTION = "call"\n',
            "    action = REFINEMENT_ACTION\n"
            "    yield action\n",
            "call",
        ),
        (
            'REFINEMENT_ACTIONS = ("allin",)\n',
            "    yield from REFINEMENT_ACTIONS\n",
            "allin",
        ),
    ],
)
def test_typed_intent_rejects_bare_action_refinement_yields(
    tmp_path,
    prefix,
    body,
    action,
):
    policy = (
        prefix
        + "def get_baseline_decision(context):\n"
        + "    legal = context['legal']\n"
        + "    betting = context['betting']\n"
        + "    opponent = context['opponent']\n"
        + "    return {'kind': 'pass'}\n\n"
        + "def iter_decisions(context, baseline, deadline):\n"
        + body
    )

    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))
    typed = _check(result, "typed_intent_v1")

    assert typed["passed"] is False
    assert any(
        location.endswith(f":bare_action_yield:{action}")
        for location in typed["evidence"]["bare_action_return_locations"]
    )


def test_typed_intent_rejects_conditional_bare_action_refinement_yields(tmp_path):
    policy = '''\
def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "pass"}

def iter_decisions(context, baseline, deadline):
    if context["hand"].get("street") == "turn":
        branch_action = "check"
        yield branch_action
    else:
        yield "fold"
'''

    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))
    typed = _check(result, "typed_intent_v1")

    assert typed["passed"] is False
    locations = typed["evidence"]["bare_action_return_locations"]
    assert any(location.endswith(":bare_action_yield:check") for location in locations)
    assert any(location.endswith(":bare_action_yield:fold") for location in locations)


def test_typed_intent_distinguishes_public_check_input_from_check_output_kind(
    tmp_path,
):
    input_policy = '''\
def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    observed = str((context["line"] or {}).get("opponent_action") or "")
    if observed == "check":
        return {"kind": "pass"}
    return {"kind": "fold"}
''' + POLICY_FOOTER
    input_result = evaluate_national_capabilities(
        _write_bot(tmp_path / "input", input_policy)
    )
    assert _check(input_result, "typed_intent_v1")["passed"] is True

    invalid_policy = '''\
def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "check"}
''' + POLICY_FOOTER
    invalid_result = evaluate_national_capabilities(
        _write_bot(tmp_path / "invalid", invalid_policy)
    )
    invalid_typed = _check(invalid_result, "typed_intent_v1")
    assert invalid_typed["passed"] is False
    assert invalid_typed["evidence"]["forbidden_kind_literals"] == ["check"]
    assert invalid_typed["evidence"]["bare_action_return_locations"] == []


def test_strict_v1_final_blueprint_passes_output_capability_probe(tmp_path):
    from system_strict_bootstrap import materialize_fresh_candidate

    bot = tmp_path / "national_v143"
    materialize_fresh_candidate(bot, final_policy=True)

    result = evaluate_national_capabilities(bot)

    assert result["ok"] is True
    typed = _check(result, "typed_intent_v1")
    assert typed["passed"] is True
    assert typed["evidence"]["bare_action_return_locations"] == []


def test_static_capability_contract_rejects_unbound_candidate_model_file(tmp_path):
    policy = '''\
def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER
    bot = _write_bot(tmp_path / "bot", policy)
    (bot / "foreign-model.bin").write_bytes(b"not-a-bound-system-asset")

    result = evaluate_national_capabilities(bot)
    layout = _check(result, "national_policy_module")

    assert result["ok"] is False
    assert layout["passed"] is False
    assert "artifact_extra_file_forbidden:foreign-model.bin" in layout["evidence"][
        "strict_artifact_layout_errors"
    ]


@pytest.mark.parametrize(
    "prefix,body",
    [
        (
            "import os as harmless\n",
            "    invoke = harmless.system\n    invoke('true')\n",
        ),
        (
            "from builtins import open as reader\n",
            "    reader('candidate-state')\n",
        ),
        (
            "import builtins as safe\n",
            "    lookup = getattr\n"
            "    reader = lookup(safe, 'open')\n"
            "    reader('candidate-state')\n",
        ),
        (
            "from pathlib import Path as Location\n",
            "    Location('candidate-state').read_text()\n",
        ),
        (
            "from subprocess import run as calculate\n",
            "    calculate(['true'])\n",
        ),
        (
            "import precompute\n",
            "    reader = precompute.__builtins__['open']\n"
            "    reader('candidate-state')\n",
        ),
    ],
)
def test_static_contract_blocks_import_and_callable_alias_io(
    tmp_path,
    prefix,
    body,
):
    policy = (
        prefix
        + "def get_baseline_decision(context):\n"
        + "    legal = context['legal']\n"
        + "    betting = context['betting']\n"
        + "    opponent = context['opponent']\n"
        + body
        + "    return {'kind': 'pass'} if 'pass' in legal['policy_kinds'] else {'kind': 'fold'}\n"
        + POLICY_FOOTER
    )
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    external_io = _check(result, "decision_path_no_external_io")
    assert external_io["passed"] is False
    assert external_io["evidence"]["locations"]


def test_static_contract_follows_context_alias_into_indirect_comprehension(
    tmp_path,
):
    policy = '''\
def summarize(events):
    return sum(1 for _event in events)


def get_baseline_decision(context):
    ctx = context
    history_key = "history"
    history_snapshot = ctx[history_key]
    actions = history_snapshot.get("actions", ())
    summarize(actions)
    legal = ctx["legal"]
    betting = ctx["betting"]
    opponent = ctx["opponent"]
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    history = _check(result, "decision_path_no_full_history_scan")
    assert history["passed"] is False
    assert any(
        "comprehension" in location
        for location in history["evidence"]["locations"]
    )


def test_static_contract_follows_copied_context_through_helper_return(tmp_path):
    policy = '''\
def extract_history(candidate_context):
    copied = dict(candidate_context)
    return copied["his" + "tory"]


def get_baseline_decision(context):
    history_snapshot = extract_history(context)
    actions = history_snapshot.get("actions", ())
    for _action in actions:
        pass
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    history = _check(result, "decision_path_no_full_history_scan")
    assert history["passed"] is False
    assert any(
        location.endswith(":for")
        for location in history["evidence"]["locations"]
    )


@pytest.mark.parametrize(
    "consumer,invocation",
    [
        (
            "def consume(events):\n"
            "    for _event in events:\n"
            "        pass\n",
            "consume(events=context['history'])",
        ),
        (
            "class Scanner:\n"
            "    def consume(self, events):\n"
            "        for _event in events:\n"
            "            pass\n",
            "Scanner().consume(context['history'])",
        ),
    ],
)
def test_static_contract_propagates_history_into_keyword_and_method_helpers(
    tmp_path,
    consumer,
    invocation,
):
    policy = consumer + f'''\

def get_baseline_decision(context):
    {invocation}
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {{"kind": "pass"}} if "pass" in legal["policy_kinds"] else {{"kind": "fold"}}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    history = _check(result, "decision_path_no_full_history_scan")
    assert history["passed"] is False
    assert any(
        location.endswith(":for")
        for location in history["evidence"]["locations"]
    )


def test_static_contract_allows_explicitly_bounded_recent_history_slice(
    tmp_path,
):
    policy = '''\
def get_baseline_decision(context):
    recent = [row for row in context["history"].get("actions", ())[-8:]]
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    if recent and "fold" in legal["policy_kinds"]:
        return {"kind": "fold"}
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    assert _check(result, "decision_path_no_full_history_scan")["passed"] is True


@pytest.mark.parametrize(
    "construction",
    [
        "table = [0] * 5000",
        "table = [(left, right) for left in range(65) for right in range(65)]",
        "table = list(range(5000))",
        "left = [0] * 3000; right = [1] * 3000; table = left + right",
    ],
)
def test_static_contract_blocks_runtime_large_table_construction(
    tmp_path,
    construction,
):
    policy = f'''\
def get_baseline_decision(context):
    {construction}
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    if table and "pass" in legal["policy_kinds"]:
        return {{"kind": "pass"}}
    return {{"kind": "fold"}}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    table_gate = _check(result, "decision_path_no_large_runtime_tables")
    assert table_gate["passed"] is False
    assert table_gate["evidence"]["locations"]


def test_static_contract_blocks_nested_mutating_table_builder(tmp_path):
    policy = '''\
def get_baseline_decision(context):
    table = []
    for left in range(65):
        for right in range(65):
            table.append((left, right))
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    table_gate = _check(result, "decision_path_no_large_runtime_tables")
    assert table_gate["passed"] is False
    assert any(
        "mutating_loop" in location
        for location in table_gate["evidence"]["locations"]
    )


@pytest.mark.parametrize(
    "helper",
    [
        '''\
def exhaustive_river_equity():
    return sum(1 for _pair in itertools.combinations(range(45), 2))
''',
        '''\
combo = itertools.combinations

def exhaustive_river_equity():
    return sum(1 for _pair in combo(range(45), 2))
''',
        '''\
def exhaustive_river_equity():
    total = 0
    for left in range(45):
        for right in range(left + 1, 45):
            total += left + right
    return total
''',
    ],
)
def test_static_contract_blocks_full_enumeration_reachable_from_baseline(
    tmp_path,
    helper,
):
    policy = '''\
import itertools

''' + helper + '''\
def get_baseline_decision(context):
    exhaustive_river_equity()
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    baseline = _check(result, "fast_policy_baseline")
    assert baseline["passed"] is False
    assert any(
        location.endswith(":baseline_full_enumeration")
        for location in baseline["evidence"]["locations"]
    )


def test_static_contract_allows_full_combinations_only_in_refinement(tmp_path):
    policy = '''\
import itertools

def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}

def iter_decisions(context, baseline, deadline):
    for _pair in itertools.combinations(range(45), 2):
        if deadline < 0:
            return
        yield baseline
        return
'''
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    assert _check(result, "fast_policy_baseline")["passed"] is True


@pytest.mark.parametrize(
    "prefix,helper",
    [
        (
            "from precompute import evaluate_seven as rank\n\n",
            "    rank = rank\n",
        ),
        (
            "import precompute\n\n",
            "    rank = precompute.best_hand_rank\n",
        ),
    ],
)
def test_static_contract_blocks_system_evaluator_aliases(
    tmp_path,
    prefix,
    helper,
):
    policy = prefix + '''\
def get_baseline_decision(context):
''' + helper + '''\
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    baseline = _check(result, "fast_policy_baseline")
    assert baseline["passed"] is False
    assert any(
        "evaluator_alias" in location
        for location in baseline["evidence"]["locations"]
    )


def test_static_contract_blocks_evaluator_alias_captured_by_helper_closure(tmp_path):
    policy = '''\
import precompute

def make_ranker():
    rank = precompute.evaluate_seven
    def rank_hole(cards):
        return rank(cards)
    return rank_hole

rank_hole = make_ranker()

def get_baseline_decision(context):
    rank_hole((0, 1, 2, 3, 4, 5, 6))
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    baseline = _check(result, "fast_policy_baseline")
    assert baseline["passed"] is False
    assert any(
        "evaluator_alias" in location
        for location in baseline["evidence"]["locations"]
    )


@pytest.mark.parametrize(
    "carrier,invoke",
    [
        (
            "EVALUATORS = [precompute.evaluate_seven]\n",
            "EVALUATORS[0]((0, 1, 2, 3, 4, 5, 6))",
        ),
        (
            "def rank(cards, evaluator=precompute.evaluate_seven):\n"
            "    return evaluator(cards)\n",
            "rank((0, 1, 2, 3, 4, 5, 6))",
        ),
        (
            "class Evaluators:\n"
            "    rank = staticmethod(precompute.evaluate_seven)\n",
            "Evaluators.rank((0, 1, 2, 3, 4, 5, 6))",
        ),
    ],
)
def test_static_contract_blocks_evaluator_value_carriers(
    tmp_path,
    carrier,
    invoke,
):
    policy = "import precompute\n\n" + carrier + (
        "def get_baseline_decision(context):\n"
        f"    {invoke}\n"
        "    legal = context[\"legal\"]\n"
        "    betting = context[\"betting\"]\n"
        "    opponent = context[\"opponent\"]\n"
        "    return {\"kind\": \"pass\"} if \"pass\" in legal[\"policy_kinds\"] else {\"kind\": \"fold\"}\n"
    ) + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    baseline = _check(result, "fast_policy_baseline")
    assert baseline["passed"] is False
    assert any(
        "evaluator_alias" in location
        for location in baseline["evidence"]["locations"]
    )


def test_static_contract_blocks_nested_deck_pair_sweep_from_baseline(tmp_path):
    policy = '''\
import precompute

def exhaustive_river_equity():
    deck = precompute.deck_without(())
    total = 0
    for left in deck:
        for right in deck:
            total += left + right
    return total

def get_baseline_decision(context):
    exhaustive_river_equity()
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    baseline = _check(result, "fast_policy_baseline")
    assert baseline["passed"] is False
    assert any(
        location.endswith(":baseline_full_enumeration")
        for location in baseline["evidence"]["locations"]
    )


def test_static_contract_blocks_direct_deck_pair_sweep_in_class_helper(tmp_path):
    policy = '''\
import precompute

class Sweep:
    def run(self):
        total = 0
        for left in precompute.deck_without(()):
            for right in precompute.deck_without(()):
                total += left + right
        return total

def get_baseline_decision(context):
    Sweep().run()
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    baseline = _check(result, "fast_policy_baseline")
    assert baseline["passed"] is False
    assert any(
        location.endswith(":baseline_full_enumeration")
        for location in baseline["evidence"]["locations"]
    )


def test_opponent_causal_use_is_a_required_base_abi_check(tmp_path):
    policy = '''\
def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    opponent = context["opponent"]
    if opponent.get("adaptation_weight", 0.0) > 0.5 and "allin" in legal["policy_kinds"]:
        return {"kind": "allin"}
    return {"kind": "pass"} if "pass" in legal["policy_kinds"] else {"kind": "fold"}
''' + POLICY_FOOTER
    result = evaluate_national_capabilities(_write_bot(tmp_path / "bot", policy))

    opponent = _check(result, "incremental_opponent_model")
    assert opponent["required"] is True
    assert opponent["passed"] is True
    assert "incremental_opponent_model" in result["required_checks"]


def test_checked_in_strict_policy_passes_hardened_static_decision_guards(
    tmp_path,
):
    result = evaluate_national_capabilities(
        _write_bot(
            tmp_path / "bot",
            BOOTSTRAP_POLICY_PATH.read_text(encoding="utf-8"),
        )
    )

    for check_id in (
        "decision_path_no_external_io",
        "decision_path_no_full_history_scan",
        "decision_path_no_large_runtime_tables",
        "incremental_opponent_model",
    ):
        assert _check(result, check_id)["passed"] is True, _check(
            result,
            check_id,
        )


def test_national_acceptance_projection_never_upgrades_skip_or_infra_to_pass():
    passed, issues, skipped = _national_acceptance_not_run("prerequisite_failed")
    assert passed is False
    assert issues == ["prerequisite_failed"]
    assert skipped == {
        "executed": False,
        "skipped": True,
        "passed": False,
        "conclusive": False,
        "outcome": "not_run",
        "reason": "prerequisite_failed",
        "issues": ["prerequisite_failed"],
    }

    passed, issues, infra = _national_acceptance_executed({
        "passed": True,
        "outcome": "infrastructure_failure",
        "issues": ["harness_failed"],
    })
    assert passed is False
    assert issues[0] == "harness_failed"
    assert "national_acceptance_report_inconsistent" in issues[1]
    assert infra["executed"] is True
    assert infra["skipped"] is False
    assert infra["conclusive"] is False

    passed, issues, conclusive = _national_acceptance_executed({
        "passed": True,
        "outcome": "passed",
        "issues": [],
    })
    assert passed is True
    assert issues == []
    assert conclusive["conclusive"] is True

    passed, issues, inconsistent = _national_acceptance_executed({
        "passed": True,
        "outcome": "passed",
        "issues": ["unexpected_wire_warning"],
    })
    assert passed is False
    assert inconsistent["report_consistent"] is False
    assert inconsistent["conclusive"] is False
    assert issues[0] == "unexpected_wire_warning"
    assert "national_acceptance_report_inconsistent" in issues[1]

    passed, issues, incomplete = _national_acceptance_executed(
        {
            "passed": True,
            "outcome": "passed",
            "hands_per_pair": 70,
            "opponents": ["national_v143"],
            "issues": [],
            "report": {"results": [{"hands_played": 69}]},
        },
        expected_hands=70,
    )
    assert passed is False
    assert incomplete["coverage_ok"] is False
    assert incomplete["conclusive"] is False
    assert "expected=70:observed=[69]" in issues[0]


def test_national_acceptance_rejects_typed_terminal_abort_even_if_marked_passed():
    from national_native import build_native_match_timing_plan

    timing_plan = build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=420.0,
    )
    forged_success = {
        "hands_played": 70,
        "native_match_timing_plan": timing_plan.snapshot(),
        "native_match_timing_plan_digest": timing_plan.digest(),
        "native_full_match_liveness_budget": timing_plan.liveness_budget_snapshot(),
        "native_match_timeout_phase": None,
        "native_terminal_abort": {
            "code": "national_20000_chip_hand_action_limit_exceeded"
        },
    }
    passed, issues, payload = _national_acceptance_executed(
        {
            "passed": True,
            "outcome": "passed",
            "hands_per_pair": 70,
            "opponents": ["national_v143"],
            "issues": [],
            "report": {"results": [forged_success]},
        },
        expected_hands=70,
        expected_timing_plan=timing_plan,
    )

    assert passed is False
    assert payload["timing_ok"] is False
    assert any("native_terminal_abort_present" in issue for issue in issues)
