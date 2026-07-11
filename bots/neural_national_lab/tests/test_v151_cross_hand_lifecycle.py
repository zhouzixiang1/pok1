from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
BOT = (
    ROOT / "bots" / "neural_national_lab" / "versions"
    / "v151_national_v150_temporal_multitask_shadow_tcp"
)


def _load_bot():
    sys.path.insert(0, str(BOT))
    spec = importlib.util.spec_from_file_location(
        "v151_temporal_national_bot", BOT / "national_bot.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_next_preflop_finalizes_exactly_one_prior_public_hand() -> None:
    module = _load_bot()
    bot = module.NativeNationalBot("TemporalTest", "upper")
    bot._send_decision = lambda _sock: None

    bot.handle("preflop|SMALLBLIND|<0,0><1,1>", object())
    bot._history = [{
        "player_id": bot._opponent_id,
        "action_type": "raise",
        "action": 300,
        "stage_bet": 300,
        "committed": 200,
        "round": 0,
        "stage": "preflop",
        "pot_after": 450,
    }]
    bot._pot = 450
    bot._last_earned = -300
    bot._last_hand_settled = True

    bot.handle("preflop|BIGBLIND|<2,2><3,3>", object())

    assert bot._hand_num == 2
    assert len(bot._cross_hand_sequence) == 1
    assert bot._cross_hand_sequence[0][4] == 1.0
    assert bot._cross_hand_sequence[0][15] == 300 / 20000
    assert bot._request()["cross_hand_sequence"] == bot._cross_hand_sequence
    bot._finalize_previous_hand()
    assert len(bot._cross_hand_sequence) == 1
