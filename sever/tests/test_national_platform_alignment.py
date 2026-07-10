import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SEVER = ROOT / "sever"
sys.path.insert(0, str(SEVER))

from engine.deck import Deck
from engine.game import GameEngine
from engine.thp_recorder import THPRecorder
from engine.validator import validate_action
from server.protocol import parse_action
from server.tcp_server import MatchManager


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


def test_parse_action_requires_exact_raise_spacing():
    assert parse_action("raise 200") == ("raise", 200)
    assert parse_action("raise  200") == ("unknown", None)
    assert parse_action("raise\t200") == ("unknown", None)
    assert parse_action(" raise 200") == ("unknown", None)
    assert parse_action("raise 200 ") == ("unknown", None)
    assert parse_action("bet 100") == ("bet", None)


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


def test_game_engine_deck_factory_makes_in_process_eval_reproducible():
    async def run_once():
        sent = []
        actions = {
            0: ["call", "call", "call", "call"],
            1: ["check", "check", "check", "check"],
        }

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


def test_game_engine_action_event_records_server_wait_and_timeout_budget():
    async def run():
        events = []

        async def send(_player_idx, _msg):
            return None

        async def broadcast(event):
            events.append(event)

        engine = GameEngine(send_func=send, broadcast_func=broadcast)
        engine.players[0].name = "A"
        engine.players[1].name = "B"
        engine.hand_num = 1

        async def recv_action(_player_idx):
            await asyncio.sleep(0)
            return "fold"

        engine._recv_action = recv_action
        await engine._run_hand(1)
        return next(event for event in events if event.get("type") == "action")

    event = asyncio.run(run())

    assert event["action"] == "fold"
    assert event["decision_wait_sec"] >= 0.0
    assert event["timeout_budget_sec"] == 60.0


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
