from pathlib import Path

from national_native import NATIVE_BOT_TEMPLATE, NATIVE_PRECOMPUTE_TEMPLATE
from national_position_contract import detect_position_semantics_errors


def _write_bot(tmp_path: Path, policy: str) -> Path:
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "national_bot.py").write_text(NATIVE_BOT_TEMPLATE, encoding="utf-8")
    (bot / "precompute.py").write_text(
        NATIVE_PRECOMPUTE_TEMPLATE,
        encoding="utf-8",
    )
    (bot / "policy.py").write_text(policy, encoding="utf-8")
    return bot


def test_typed_position_and_line_fields_pass(tmp_path):
    bot = _write_bot(
        tmp_path,
        '''\
def get_baseline_decision(context):
    hand = context["hand"]
    line = context["line"]
    if hand["acts_first_postflop"] and hand["position"] == "big_blind":
        return {"kind": "pass"}
    if line["hero_in_position_postflop"] and line["position"] == "small_blind":
        return {"kind": "pass"}
    if line["can_donk"] or line["can_delayed_probe"]:
        return {"kind": "raise", "raise_to": context["legal"]["min_raise_to"]}
    return {"kind": "fold"}

def iter_decisions(context, baseline, deadline):
    return iter(())
''',
    )
    assert detect_position_semantics_errors(bot) == []


def test_retired_seat_reconstruction_is_rejected(tmp_path):
    old_name = "dealer" + "_id"
    bot = _write_bot(
        tmp_path,
        f'''\
def get_baseline_decision(context):
    {old_name} = context["{old_name}"]
    return {{"kind": "pass"}} if {old_name} == 0 else {{"kind": "fold"}}

def iter_decisions(context, baseline, deadline):
    return iter(())
''',
    )
    errors = detect_position_semantics_errors(bot)
    assert any("retired position identifier" in error for error in errors)
    assert any("retired decision_context key" in error for error in errors)


def test_contradictory_postflop_derivations_are_rejected(tmp_path):
    bot = _write_bot(
        tmp_path,
        '''\
def get_baseline_decision(context):
    position = context["hand"]["position"]
    acts_first_postflop = position == "small_blind"
    hero_in_position_postflop = position == "big_blind"
    return {"kind": "pass"} if acts_first_postflop or hero_in_position_postflop else {"kind": "fold"}

def iter_decisions(context, baseline, deadline):
    return iter(())
''',
    )
    errors = detect_position_semantics_errors(bot)
    assert any("acts_first_postflop" in error for error in errors)
    assert any("hero_in_position_postflop" in error for error in errors)


def test_system_runtime_and_comments_are_not_candidate_position_evidence(tmp_path):
    bot = _write_bot(
        tmp_path,
        '''\
# Historical prose in a comment is inert and should not create a false positive.
def get_baseline_decision(context):
    return {"kind": "pass"}

def iter_decisions(context, baseline, deadline):
    return iter(())
''',
    )
    assert detect_position_semantics_errors(bot) == []
