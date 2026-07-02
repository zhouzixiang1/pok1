from pathlib import Path

from core import decision_tester
from core.tool_gates import detect_position_semantics_errors


def test_decision_templates_use_bb_first_postflop_semantics():
    covers = {tpl["_covers"]: tpl for tpl in decision_tester.TEMPLATE_SCENARIOS}

    assert "flop_sb_act_first" not in covers
    assert covers["flop_bb_act_first"]["input"]["my_id"] == 1
    assert covers["flop_bb_act_first"]["input"]["dealer_id"] == 0
    assert covers["turn_bb_act_first_twopair"]["input"]["my_id"] == 1
    assert covers["turn_bb_act_first_twopair"]["input"]["dealer_id"] == 0


def test_constant_template_map_has_no_old_postflop_position_key():
    assert "flop_sb_act_first" not in set(decision_tester._CONSTANT_TEMPLATE_MAP.values())
    assert decision_tester._CONSTANT_TEMPLATE_MAP["RAISE_RATIO"] == "flop_bb_act_first"
    assert decision_tester._CONSTANT_TEMPLATE_MAP["CALL_MARGIN"] == "flop_sb_vs_lead"


def test_position_semantics_gate_flags_old_formula(tmp_path):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    (bot_dir / "state.py").write_text(
        "def f(dealer_id):\n"
        "    sb = next_player(dealer_id, 1)\n"
        "    bb = next_player(dealer_id, 2)\n"
        "    return sb, bb\n",
        encoding="utf-8",
    )

    errors = detect_position_semantics_errors(bot_dir)
    assert any("SB must be dealer_id" in err for err in errors)
    assert any("BB must be 1 - dealer_id" in err for err in errors)


def test_position_semantics_gate_accepts_current_formula(tmp_path):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    (bot_dir / "state.py").write_text(
        "def f(dealer_id):\n"
        "    sb = dealer_id\n"
        "    bb = 1 - dealer_id\n"
        "    return sb, bb\n",
        encoding="utf-8",
    )

    assert detect_position_semantics_errors(bot_dir) == []


def test_prompts_do_not_claim_bb_is_postflop_in_position():
    root = Path(__file__).resolve().parents[1] / "core" / "prompts"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.md"))
    assert "BB postflop in-position" not in text
    assert "SB acts first every street" not in text
