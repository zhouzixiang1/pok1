import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

from archive.botzone_local.sever.bot_adapter import (
    BotAdapter,
    int_to_tcp_card_str,
    tcp_card_to_int,
)
from sever.engine.deck import Deck, str_to_card
from sever.engine.game import GameEngine
from sever.engine.thp_recorder import THPRecorder
from sever.engine.validator import validate_action
from sever.server.protocol import parse_action
from sever.server.tcp_server import MatchManager


def _state(**overrides):
    state = {
        "stage": "flop",
        "actions": [],
        "player_chips": 20000,
        "player_bet": 0,
        "opponent_bet": 0,
        "is_small_blind": True,
        "is_big_blind": False,
        "allin_occurred": False,
        "player_action_count": 0,
    }
    state.update(overrides)
    return state


def _load_module_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _assert_postflop_pass_is_recorded_as_call(judge_module):
    holdem = judge_module.Holdem([20000, 20000], dealer_idx=0)
    holdem.deal_cards_and_blind()

    assert holdem.player_action(0) == []  # SB calls preflop
    assert holdem.player_action(0) is None  # BB checks; move to flop
    assert holdem.round == judge_module.Holdem.FLOP

    assert holdem.player_action(0) == []  # BB checks first postflop
    assert holdem.player_action(0) is None  # SB sends Botzone 0 to pass street

    assert holdem.history[-2]["round"] == judge_module.Holdem.FLOP
    assert holdem.history[-2]["action_type"] == "check"
    assert holdem.history[-1]["round"] == judge_module.Holdem.FLOP
    assert holdem.history[-1]["action"] == 0
    assert holdem.history[-1]["action_type"] == "call"


def _write_call_bot(bot_dir: Path):
    bot_dir.mkdir(parents=True)
    (bot_dir / "main.py").write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    print(json.dumps({'response': 0}), flush=True)\n",
        encoding="utf-8",
    )


class _FakeBotProcess:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def send_and_recv(self, payload, timeout=60):
        self.payloads.append(json.loads(json.dumps(payload)))
        if not self.responses:
            return {"response": 0}
        return self.responses.pop(0)


class _CaptureAdapter(BotAdapter):
    def __init__(self, responses):
        super().__init__("127.0.0.1", 10001, "unused", "Bot")
        self.bot = _FakeBotProcess(responses)
        self.sent = []

    async def _send_line(self, msg):
        self.sent.append(msg)


def test_parse_action_requires_exact_raise_spacing():
    assert parse_action("raise 200") == ("raise", 200)
    assert parse_action("raise  200") == ("unknown", None)
    assert parse_action("raise\t200") == ("unknown", None)
    assert parse_action(" raise 200") == ("unknown", None)
    assert parse_action("raise 200 ") == ("unknown", None)
    assert parse_action("bet 100") == ("bet", None)


def test_adapter_card_mapping_round_trips_all_52_cards():
    for card_int in range(52):
        tcp_text = int_to_tcp_card_str(card_int)
        assert tcp_card_to_int(str_to_card(tcp_text)) == card_int


def test_postflop_after_check_requires_call_not_second_check():
    checked = _state(actions=[("check", None)])

    ok, reason = validate_action("check", None, checked)
    assert not ok
    assert "check is illegal" in reason

    assert validate_action("call", None, checked) == (True, "")


def test_postflop_first_raise_after_check_has_minimum_and_positive_amount():
    checked = _state(actions=[("check", None)])

    for amount in (-100, 0, 50, 99):
        ok, _ = validate_action("raise", amount, checked)
        assert not ok

    assert validate_action("raise", 100, checked) == (True, "")


def test_thp_hand_line_uses_big_blind_order_for_cards_earnings_and_players():
    recorder = THPRecorder(team_a_name="A", team_b_name="B")
    recorder.on_hand_start(hand_num=1, sb_idx=0, bb_idx=1)
    recorder.on_hand_cards(0, [(0, 12), (1, 12)])
    recorder.on_hand_cards(1, [(2, 0), (3, 0)])
    recorder.on_settle([-50, 50])

    line = recorder.format_hand(recorder.records[0])
    assert line.endswith(":50|-50:B|A;")


def test_bot_adapter_maps_zero_after_postflop_check_to_call():
    adapter = BotAdapter("127.0.0.1", 10001, "unused", "Bot")
    adapter._stage = "flop"
    adapter._is_sb = True
    adapter._my_id = 0
    adapter._history = [
        {"round": 1, "player_id": 1, "action": 0, "action_type": "check"}
    ]

    assert adapter._convert_action(0) == ("call", "call", None)
    assert adapter.telemetry["postflop_pass_conversions"] == 1


def test_bot_adapter_maps_zero_as_first_postflop_action_to_check():
    adapter = BotAdapter("127.0.0.1", 10001, "unused", "Bot")
    adapter._stage = "flop"
    adapter._is_sb = False
    adapter._my_id = 1
    adapter._history = []

    assert adapter._convert_action(0) == ("check", "check", None)


def test_bot_adapter_payload_accumulates_across_national_match():
    async def run():
        adapter = _CaptureAdapter([
            {"response": 0, "data": {"memory": "kept"}},
            {"response": 0},
        ])
        await adapter._handle("preflop|SMALLBLIND|<0,0><1,1>")
        await adapter._handle("earnChips 100")
        await adapter._handle("oppo_hands|<2,2><3,3>")
        await adapter._handle("preflop|BIGBLIND|<0,4><1,5>")
        await adapter._handle("call")
        return adapter

    adapter = asyncio.run(run())
    assert len(adapter.bot.payloads) == 2

    first, second = adapter.bot.payloads
    assert len(first["requests"]) == 1
    assert first["responses"] == []
    assert first["requests"][0]["my_id"] == 0
    assert first["requests"][0]["dealer_id"] == 0

    assert len(second["requests"]) == 2
    assert second["responses"] == [0]
    assert second["data"] == {"memory": "kept"}
    assert second["requests"][1]["my_id"] == 0
    assert second["requests"][1]["dealer_id"] == 1
    assert second["requests"][1]["total_win_chips"] == [100, -100]
    assert len(second["requests"][1]["opponent_showdowns"]) == 1
    assert second["requests"][1]["opponent_showdowns"][0]["opponent_cards"] == [
        tcp_card_to_int(str_to_card("<2,2>")),
        tcp_card_to_int(str_to_card("<3,3>")),
    ]
    assert second["requests"][1]["history"][0]["player_id"] == 1
    assert adapter._total_win_games == [1, 0]


def test_bot_adapter_reconstructs_preflop_allin_price_exactly():
    async def run():
        adapter = _CaptureAdapter([{"response": 0}])
        await adapter._handle("preflop|BIGBLIND|<0,4><1,5>")
        await adapter._handle("allin")
        return adapter

    adapter = asyncio.run(run())
    payload = adapter.bot.payloads[0]
    request = payload["requests"][0]

    assert request["opponent_chips"] == 0
    assert request["my_stage_bet"] == 100
    assert request["opponent_stage_bet"] == 20000
    assert request["to_call"] == 19900
    assert request["pot"] == 20100
    assert request["opponent_allin"] is True
    assert adapter.sent == ["call"]
    assert adapter._my_chips == 0
    assert adapter._my_stage_bet == 20000
    assert adapter._pot == 40000
    assert adapter._in_allin_runout is True


def test_bot_adapter_maps_minus_two_facing_opponent_allin_to_call():
    async def run():
        adapter = _CaptureAdapter([{"response": -2}])
        await adapter._handle("preflop|BIGBLIND|<0,4><1,5>")
        await adapter._handle("allin")
        return adapter

    adapter = asyncio.run(run())

    assert adapter.sent == ["call"]
    assert adapter.telemetry["allin_conversions"] == 1
    assert adapter._my_chips == 0
    assert adapter._my_stage_bet == 20000
    assert adapter._pot == 40000
    assert adapter._in_allin_runout is True


def test_bot_adapter_keeps_minus_two_as_open_allin():
    adapter = BotAdapter("127.0.0.1", 10001, "unused", "Bot")
    adapter._stage = "flop"
    adapter._my_stage_bet = 0
    adapter._opponent_stage_bet = 0
    adapter._my_chips = 20000
    adapter._opponent_chips = 20000

    assert adapter._convert_action(-2) == ("allin", "allin", None)
    assert adapter.telemetry["allin_conversions"] == 0


def test_bot_adapter_short_opponent_allin_does_not_overcharge_call():
    async def run():
        adapter = _CaptureAdapter([{"response": 0}])
        adapter._stage = "flop"
        adapter._hand_num = 1
        adapter._my_id = 0
        adapter._opponent_id = 1
        adapter._dealer_id = 1
        adapter._my_chips = 1500
        adapter._opponent_chips = 500
        adapter._my_stage_bet = 1000
        adapter._opponent_stage_bet = 1000
        adapter._pot = 3000
        await adapter._handle("allin")
        return adapter

    adapter = asyncio.run(run())
    request = adapter.bot.payloads[0]["requests"][0]

    assert request["opponent_chips"] == 0
    assert request["opponent_stage_bet"] == 1500
    assert request["to_call"] == 500
    assert request["pot"] == 3500
    assert adapter.sent == ["call"]
    assert adapter._my_chips == 1000
    assert adapter._my_stage_bet == 1500
    assert adapter._pot == 4000
    assert adapter._in_allin_runout is True


def test_game_engine_deck_factory_makes_in_process_eval_reproducible():
    async def run_once():
        sent = []
        actions = {0: ["call", "call", "call", "call"], 1: ["check", "check", "check", "check"]}

        async def send(player_idx, msg):
            sent.append((player_idx, msg))

        engine = GameEngine(
            send_func=send,
            deck_factory=lambda hand_num: Deck(seed=1234 + hand_num),
        )
        engine.players[0].name = "A"
        engine.players[1].name = "B"

        async def recv_action(player_idx):
            return actions[player_idx].pop(0)

        engine._recv_action = recv_action
        await engine._run_hand(1)
        return [msg for _, msg in sent if msg.startswith("preflop|")]

    assert asyncio.run(run_once()) == asyncio.run(run_once())


def test_local_and_web_judges_record_postflop_pass_as_call():
    root_judge = _load_module_from_path(
        "root_judge_for_national_test",
        ROOT / "archive" / "botzone_local" / "engine" / "judge.py",
    )
    web_judge = _load_module_from_path(
        "web_core_judge_for_national_test",
        ROOT / "archive" / "botzone_local" / "web_core" / "engine" / "judge.py",
    )

    _assert_postflop_pass_is_recorded_as_call(root_judge)
    _assert_postflop_pass_is_recorded_as_call(web_judge)


def test_bot_adapter_telemetry_counts_invalid_actions():
    adapter = BotAdapter("127.0.0.1", 10001, "unused", "Bot")

    assert adapter._convert_action("raise 200") == ("fold", "fold", None)
    assert adapter.telemetry["invalid_actions"] == 1

    assert adapter._convert_action(-3) == ("fold", "fold", None)
    assert adapter.telemetry["invalid_actions"] == 2


def test_bot_adapter_converts_raise_using_all_chips_to_allin():
    adapter = BotAdapter("127.0.0.1", 10001, "unused", "Bot")
    adapter._stage = "flop"
    adapter._my_stage_bet = 0
    adapter._my_chips = 100

    assert adapter._convert_action(100) == ("allin", "allin", None)
    assert adapter.telemetry["allin_conversions"] == 1


def test_bot_adapter_telemetry_counts_clamped_raise():
    adapter = BotAdapter("127.0.0.1", 10001, "unused", "Bot")
    adapter._stage = "flop"
    adapter._my_stage_bet = 0
    adapter._my_chips = 20000

    assert adapter._convert_action(50) == ("raise 100", "raise", 100)
    assert adapter.telemetry["clamped_raises"] == 1
    assert adapter.telemetry["would_be_illegal_raise"] == 1


def test_bot_adapter_distinguishes_exact_2x_legality_from_plus_one_policy():
    adapter = BotAdapter("127.0.0.1", 10001, "unused", "Bot")
    adapter._stage = "flop"
    adapter._my_stage_bet = 100
    adapter._opponent_stage_bet = 200
    adapter._my_chips = 20000
    adapter._history = [
        {"round": 1, "player_id": 1, "action": 200, "action_type": "raise"},
    ]

    # The bridge retains +1 headroom for legacy compatibility, but exact 2x
    # is official-legal and therefore must not increment illegality telemetry.
    assert adapter._convert_action(400) == ("raise 401", "raise", 401)
    assert adapter.telemetry["clamped_raises"] == 1
    assert adapter.telemetry["would_be_illegal_raise"] == 0


def test_national_acceptance_matrix_skips_incomplete_default_national_bots(tmp_path, monkeypatch):
    matrix = _load_module_from_path(
        "national_acceptance_matrix_default_test",
        ROOT / "archive" / "botzone_local" / "scripts" / "national_acceptance_matrix.py",
    )
    completed = tmp_path / "bots" / "national_v10"
    incomplete = tmp_path / "bots" / "national_v11"
    _write_call_bot(completed)
    _write_call_bot(incomplete)
    (completed / ".completed").write_text("", encoding="utf-8")

    ratings_dir = tmp_path / "web" / "core" / "results"
    ratings_dir.mkdir(parents=True)
    (ratings_dir / "glicko_ratings.json").write_text(
        json.dumps({
            "national_v11": {"r": 3000, "rd": 30},
            "national_v10": {"r": 1200, "rd": 50},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(matrix, "ROOT", tmp_path)

    assert [bot.label for bot in matrix.default_bots(limit=2)] == ["national_v10"]


def test_national_acceptance_strict_blocks_adapter_allin_conversion():
    matrix = _load_module_from_path(
        "national_acceptance_matrix_strict_test",
        ROOT / "archive" / "botzone_local" / "scripts" / "national_acceptance_matrix.py",
    )

    telemetry = {"allin_conversions": 1}

    assert matrix._na._critical_adapter_issues(telemetry, strict=True) == [
        "allin_conversions=1"
    ]
    assert matrix._na._critical_adapter_issues(telemetry, strict=False) == []


def test_national_acceptance_matrix_runs_bots_through_adapter_and_game(tmp_path):
    matrix = _load_module_from_path(
        "national_acceptance_matrix_run_test",
        ROOT / "archive" / "botzone_local" / "scripts" / "national_acceptance_matrix.py",
    )
    bot_a = tmp_path / "CallA"
    bot_b = tmp_path / "CallB"
    _write_call_bot(bot_a)
    _write_call_bot(bot_b)

    report = asyncio.run(matrix.run_matrix(
        [
            matrix.BotSpec("CallA", bot_a),
            matrix.BotSpec("CallB", bot_b),
        ],
        hands=2,
    ))

    assert report["results"][0]["hands_played"] == 2
    assert report["results"][0]["passed_compliance"]
    assert report["summary"]["CallA"]["passed_compliance"]
    assert report["summary"]["CallB"]["passed_compliance"]
    assert report["summary"]["CallA"]["illegal_actions"] == 0
    assert report["summary"]["CallB"]["timeouts"] == 0

    markdown = matrix.format_markdown(report)
    assert "Pairwise Net Chips Per Hand" in markdown
    assert "PASS" in markdown


def test_allin_runout_records_public_cards_in_thp():
    async def run():
        sent = []
        actions = {0: ["allin"], 1: ["call"]}
        recorder = THPRecorder("A", "B")

        async def send(player_idx, msg):
            sent.append((player_idx, msg))

        engine = GameEngine(
            send_func=send,
            recorder=recorder,
        )
        engine.players[0].name = "A"
        engine.players[1].name = "B"

        async def recv_action(player_idx):
            return actions[player_idx].pop(0)

        engine._recv_action = recv_action
        result = await engine._run_hand(1)
        return result, sent, recorder

    result, sent, recorder = asyncio.run(run())

    assert result.is_showdown
    assert any(msg.startswith("flop|") for _, msg in sent)
    assert any(msg.startswith("turn|") for _, msg in sent)
    assert any(msg.startswith("river|") for _, msg in sent)

    rec = recorder.records[0]
    assert len(rec.flop_cards) == 3
    assert rec.turn_card is not None
    assert rec.river_card is not None


def test_match_manager_auto_starts_after_second_client_connects():
    class FakeWriter:
        def __init__(self):
            self.closed = False
            self.writes = []

        def get_extra_info(self, name):
            if name == "peername":
                return ("127.0.0.1", 10001)
            return None

        def write(self, data):
            self.writes.append(data)

        async def drain(self):
            pass

        def close(self):
            self.closed = True

    async def run():
        manager = MatchManager()
        readers = [asyncio.StreamReader(), asyncio.StreamReader()]
        writers = [FakeWriter(), FakeWriter()]

        await manager.handle_new_connection(readers[0], writers[0])
        assert manager._match_task is None

        await manager.handle_new_connection(readers[1], writers[1])
        await asyncio.sleep(0)

        try:
            assert manager._match_task is not None
            assert not manager._match_task.done()
            assert writers[0].writes == [b"name\n"]
            assert writers[1].writes == [b"name\n"]
        finally:
            manager._match_task.cancel()
            try:
                await manager._match_task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
