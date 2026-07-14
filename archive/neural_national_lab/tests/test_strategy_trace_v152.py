from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
VERSIONS = ROOT / "bots" / "neural_national_lab" / "versions"
V140 = VERSIONS / "v140_national_v123_overlay_no_large_commit_veto_tcp"
V152 = VERSIONS / "v152_national_v140_strategy_context_trace_tcp"


def _requests() -> list[dict]:
    common = {
        "num_players": 2,
        "my_id": 0,
        "dealer_id": 0,
        "my_cards": [48, 45],
        "my_chips": 19_900,
        "opponent_chips": 19_900,
        "opponent_allin": False,
        "hand": 0,
        "max_hand": 70,
        "remaining_hands": 69,
        "total_win_chips": [0, 0],
        "total_win_games": [0, 0],
        "opponent_showdowns": [],
    }
    preflop = {
        **common,
        "public_cards": [],
        "history": [],
        "my_stage_bet": 50,
        "opponent_stage_bet": 100,
        "pot": 150,
        "to_call": 50,
    }
    flop = {
        **common,
        "public_cards": [0, 5, 10],
        "history": [
            {
                "round": 0,
                "player_id": 0,
                "action": 0,
                "action_type": "call",
                "stage_bet": 100,
                "chips_after": 19_900,
                "committed": 50,
            },
            {
                "round": 0,
                "player_id": 1,
                "action": 0,
                "action_type": "check",
                "stage_bet": 100,
                "chips_after": 19_900,
                "committed": 0,
            },
            {
                "round": 1,
                "player_id": 1,
                "action": 0,
                "action_type": "check",
                "stage_bet": 0,
                "chips_after": 19_900,
                "committed": 0,
            },
        ],
        "my_stage_bet": 0,
        "opponent_stage_bet": 0,
        "pot": 200,
        "to_call": 0,
    }
    return [preflop, flop]


def _strategy_run(version: Path, *, trace: bool) -> list[dict]:
    script = r'''
import json, random
from strategy import get_action
if __TRACE__:
    from strategy_trace import consume_strategy_context
requests = json.loads(__REQUESTS__)
random.seed(20260711)
rows = []
for request in requests:
    action = get_action(request, [request])
    row = {"action": action}
    if __TRACE__:
        row["context"] = consume_strategy_context()
    rows.append(row)
print(json.dumps(rows, separators=(",", ":")))
'''.replace("__TRACE__", "True" if trace else "False").replace(
        "__REQUESTS__", repr(json.dumps(_requests()))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=version,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    return json.loads(completed.stdout)


def test_trace_encoding_does_not_change_v140_rule_actions() -> None:
    baseline = _strategy_run(V140, trace=False)
    traced = _strategy_run(V152, trace=True)

    assert [row["action"] for row in traced] == [
        row["action"] for row in baseline
    ]


def test_trace_contains_exact_preflop_and_postflop_rule_context() -> None:
    rows = _strategy_run(V152, trace=True)
    preflop = rows[0]["context"]
    postflop = rows[1]["context"]

    for context in (preflop, postflop):
        assert context["schema"] == "v140_strategy_context_v1"
        assert context["dim"] == 66
        assert context["available"] is True
        assert len(context["features"]) == 66
        assert all(0.0 <= value <= 1.0 for value in context["features"])
        assert len(context["raw"]["range_summary"]) == 6
        assert "range_weights" not in context["raw"]
    assert preflop["raw"]["preflop_strength"] is not None
    assert "draw_profile" not in preflop["raw"]
    assert postflop["raw"]["preflop_strength"] is None
    assert postflop["raw"]["draw_profile"]
    assert postflop["raw"]["value_plan"]


def test_native_trace_row_embeds_strategy_context() -> None:
    script = r'''
import contextlib, io, json, random
from national_bot import NativeNationalBot, TRACE_PREFIX
bot = NativeNationalBot("TraceBot", "upper")
bot._trace_enabled = True
bot._hand_num = 1
bot._my_cards = [48, 45]
bot._is_sb = True
bot._my_stage_bet = 50
bot._opponent_stage_bet = 100
bot._pot = 150
random.seed(20260711)
captured = io.StringIO()
with contextlib.redirect_stderr(captured):
    bot._strategy_action(0)
rows = [
    json.loads(line[len(TRACE_PREFIX):])
    for line in captured.getvalue().splitlines()
    if line.startswith(TRACE_PREFIX)
]
print(json.dumps(rows, separators=(",", ":")))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=V152,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    rows = json.loads(completed.stdout)

    assert len(rows) == 1
    assert rows[0]["strategy_context"]["schema"] == (
        "v140_strategy_context_v1"
    )
    assert len(rows[0]["strategy_context"]["features"]) == 66
