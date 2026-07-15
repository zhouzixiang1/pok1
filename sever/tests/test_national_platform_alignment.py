import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sever.engine.deck import Deck
from sever.engine.game import GameEngine
from sever.engine.thp_recorder import THPRecorder
from sever.engine.validator import validate_action
from sever.server.protocol import (
    parse_action,
    split_client_actions,
    split_server_messages,
)
from sever.server.tcp_server import ClientConnection, MatchManager


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


def test_client_action_tokenizer_handles_sticky_and_defers_numeric_boundary():
    messages, rest = split_client_actions(
        "raise 200call",
        flush_boundary=False,
    )
    assert messages == ["raise 200"]
    assert rest == "call"

    messages, rest = split_client_actions(rest, flush_boundary=True)
    assert messages == ["call"]
    assert rest == ""

    messages, rest = split_client_actions(
        "callraise 400fold",
        flush_boundary=True,
    )
    assert messages == ["call", "raise 400", "fold"]
    assert rest == ""

    assert split_client_actions("raise 2", flush_boundary=False) == (
        [],
        "raise 2",
    )
    assert split_client_actions("raise 200", flush_boundary=True) == (
        ["raise 200"],
        "",
    )


def test_client_action_tokenizer_preserves_illegal_spacing_and_trailing_bytes():
    for raw in ("raise  200", "raise\t200", "callx", "fold ", "\ncall"):
        assert split_client_actions(raw, flush_boundary=True) == ([], raw)


def test_server_message_tokenizer_handles_fragmented_and_sticky_raw_tokens():
    raw = "earnChips -100preflop|SMALLBLIND|<0,3><1,12>raise 200call"
    messages, rest = split_server_messages(raw, flush_boundary=True)
    assert messages == [
        "earnChips -100",
        "preflop|SMALLBLIND|<0,3><1,12>",
        "raise 200",
        "call",
    ]
    assert rest == ""


class _MemoryWriter:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def test_raw_client_connection_accepts_no_newline_and_bytewise_fragmentation():
    async def run():
        reader = asyncio.StreamReader()
        writer = _MemoryWriter()
        connection = ClientConnection(reader, writer, idle_flush_sec=0.01)
        pending = asyncio.create_task(connection.recv_action(timeout=1.0))
        for byte in b"raise 200":
            reader.feed_data(bytes([byte]))
            await asyncio.sleep(0)
        result = await pending
        await connection.send_message("call")
        return result, writer.writes

    result, writes = asyncio.run(run())
    assert result == "raise 200"
    assert writes == [b"call"]


def test_raw_client_connection_does_not_reuse_unsolicited_sticky_action():
    async def run():
        reader = asyncio.StreamReader()
        writer = _MemoryWriter()
        connection = ClientConnection(reader, writer, idle_flush_sec=0.005)
        pending = asyncio.create_task(connection.recv_action(timeout=1.0))
        reader.feed_data(b"raise 200call")
        first = await pending
        second = await connection.recv_action(timeout=0.02)
        return first, second

    first, second = asyncio.run(run())
    assert first == "protocol_multiple_actions:raise 200|call"
    assert parse_action(first) == ("unknown", None)
    assert second is None


def test_raw_client_connection_name_handshake_uses_idle_not_newline():
    async def run():
        reader = asyncio.StreamReader()
        connection = ClientConnection(reader, _MemoryWriter(), idle_flush_sec=0.01)
        pending = asyncio.create_task(connection.recv_name(timeout=1.0))
        for fragment in (b"Nat", b"ional", b"Bot"):
            reader.feed_data(fragment)
            await asyncio.sleep(0)
        return await pending

    assert asyncio.run(run()) == "NationalBot"


def test_raw_client_connection_reassembles_fragmented_utf8_team_name():
    async def run():
        reader = asyncio.StreamReader()
        connection = ClientConnection(reader, _MemoryWriter(), idle_flush_sec=0.01)
        pending = asyncio.create_task(connection.recv_name(timeout=1.0))
        encoded = "底牌码农".encode("utf-8")
        for fragment in (encoded[:2], encoded[2:7], encoded[7:]):
            reader.feed_data(fragment)
            await asyncio.sleep(0)
        return await pending

    assert asyncio.run(run()) == "底牌码农"


def test_raw_client_connection_does_not_idle_flush_inside_utf8_codepoint():
    async def run():
        reader = asyncio.StreamReader()
        connection = ClientConnection(reader, _MemoryWriter(), idle_flush_sec=0.01)
        pending = asyncio.create_task(connection.recv_name(timeout=1.0))
        encoded = "国赛".encode("utf-8")
        reader.feed_data(encoded[:4])  # complete 国 + first byte of 赛
        await asyncio.sleep(0.03)      # longer than the idle boundary
        reader.feed_data(encoded[4:])
        return await pending

    assert asyncio.run(run()) == "国赛"


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


def test_official_oracle_accepts_exact_2x_reraise_and_rejects_below_boundary():
    fixture_path = ROOT / "sever" / "tests" / "fixtures" / "official_raise_boundary_oracle_20260711.json"
    oracle = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert oracle["official_exe_sha256"] == (
        "9d01b443d4920a7e06a487d87ea1b050ea2ca5359023602f98c3c236c734e81a"
    )
    assert oracle["exact_2x_raw_wire_sha256"] == (
        "dc9dffa1121bee77bab1478842b7f336e1d4a72686e2ad7cbf322ed077bf85f3"
    )
    assert {row["big_blind_conn"] for row in oracle["observations"]} == {"A", "B"}
    assert all(sum(row["settlements"].values()) == 0 for row in oracle["observations"])

    facing_raise = _state(
        stage="preflop",
        actions=[("raise", 200)],
        player_bet=100,
        opponent_bet=200,
        is_small_blind=False,
        is_big_blind=True,
        player_action_count=0,
    )
    assert validate_action("raise", 400, facing_raise) == (True, "")
    ok, reason = validate_action("raise", 399, facing_raise)
    assert not ok
    assert ">= 2x" in reason


def test_equal_wealth_invariant_rules_out_short_allin_facing_larger_wager():
    # Every legal street starts with equal chips+street_bet wealth. call,
    # raise, and allin only move chips into street_bet, so the equality stays
    # true until the street terminates. A 500 stack at bet=0 facing 1000 is
    # therefore not reachable from the national 20000/20000 hand reset.
    wealth = 20_000
    for player_bet in (0, 50, 500, 1_000, 19_999):
        player_chips = wealth - player_bet
        for opponent_bet in (player_bet + 1, 1_000, 19_999):
            if opponent_bet <= player_bet or opponent_bet > wealth:
                continue
            assert opponent_bet - player_bet <= player_chips

    # The equal-wealth form of "500 behind facing 1000" has 500 already on
    # the table and means the opponent is all-in. The only legal continuation
    # is call/fold; a second allin token remains forbidden by rule 13.
    facing_prior_allin = _state(
        stage="preflop",
        actions=[("allin", 1_000)],
        player_chips=500,
        player_bet=500,
        opponent_bet=1_000,
        is_small_blind=False,
        is_big_blind=True,
        allin_occurred=True,
        player_action_count=0,
    )
    assert validate_action("call", None, facing_prior_allin) == (True, "")
    ok, reason = validate_action("allin", None, facing_prior_allin)
    assert ok is False
    assert "consecutive allin" in reason
    assert min(
        max(
            0,
            facing_prior_allin["opponent_bet"]
            - facing_prior_allin["player_bet"],
        ),
        facing_prior_allin["player_chips"],
    ) == 500


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
        return events

    events = asyncio.run(run())
    request = next(event for event in events if event.get("type") == "action_requested")
    event = next(event for event in events if event.get("type") == "action")

    assert request["player_idx"] == 0
    assert request["deadline_epoch_ms"] > 0
    assert request["timeout_budget_sec"] == 60.0
    assert event["action"] == "fold"
    assert event["decision_wait_sec"] >= 0.0
    assert event["timeout_budget_sec"] == 60.0


def test_game_engine_matches_official_omitted_street_closers():
    async def run_round(stage, first_idx, second_idx, first_bet, second_bet, actions):
        sent = []
        events = []

        async def send(player_idx, message):
            sent.append((player_idx, message))

        async def broadcast(event):
            events.append(event)

        engine = GameEngine(send_func=send, broadcast_func=broadcast)
        engine.hand_num = 1
        engine.players[0].blind_type = "SMALLBLIND"
        engine.players[1].blind_type = "BIGBLIND"

        async def recv_action(player_idx):
            return actions[player_idx].pop(0)

        engine._recv_action = recv_action
        await engine._betting_round(
            stage=stage,
            first_idx=first_idx,
            second_idx=second_idx,
            first_bet=first_bet,
            second_bet=second_bet,
            pot=first_bet + second_bet,
            community=[],
            deck=Deck(seed=7),
        )
        return sent, events

    preflop_sent, preflop_events = asyncio.run(run_round(
        "preflop", 0, 1, 50, 100, {0: ["call"], 1: ["check"]},
    ))
    assert preflop_sent == [(1, "call")]
    assert [event["wire_relayed"] for event in preflop_events if event["type"] == "action"] == [True, False]

    postflop_sent, postflop_events = asyncio.run(run_round(
        "flop", 1, 0, 0, 0, {0: ["call"], 1: ["check"]},
    ))
    assert postflop_sent == [(0, "check")]
    assert [event["wire_relayed"] for event in postflop_events if event["type"] == "action"] == [True, False]


def test_game_engine_matches_official_hand_70_wire_settlement_boundary():
    async def settle(hand_num):
        sent = []
        events = []

        async def send(player_idx, message):
            sent.append((player_idx, message))

        async def broadcast(event):
            events.append(event)

        engine = GameEngine(send_func=send, broadcast_func=broadcast)
        engine.hand_num = hand_num
        engine.players[0].chips = 19_950
        engine.players[1].chips = 19_900
        result = await engine._settle_fold(winner_idx=1, pot=150, community=[])
        return result, sent, events

    _result_69, sent_69, events_69 = asyncio.run(settle(69))
    result_70, sent_70, events_70 = asyncio.run(settle(70))

    assert sent_69 == [(0, "earnChips -50"), (1, "earnChips 50")]
    assert sent_70 == []
    assert result_70.earnings == (-50, 50)
    assert next(event for event in events_69 if event["type"] == "settle")["wire_settlement_relayed"] is True
    assert next(event for event in events_70 if event["type"] == "settle")["wire_settlement_relayed"] is False


def test_game_engine_observer_hole_cards_do_not_change_tcp_payloads():
    async def run():
        sent = []
        events = []

        async def send(player_idx, message):
            sent.append((player_idx, message))

        async def broadcast(event):
            events.append(event)

        engine = GameEngine(send_func=send, broadcast_func=broadcast)
        engine.players[0].name = "A"
        engine.players[1].name = "B"

        async def recv_action(_player_idx):
            return "fold"

        engine._recv_action = recv_action
        await engine._run_hand(1)
        cards = next(event for event in events if event.get("type") == "cards_dealt")
        return sent, cards

    sent, cards = asyncio.run(run())
    assert len(cards["hole_cards"]) == 2
    assert all(len(hand) == 2 for hand in cards["hole_cards"])
    assert all("hole_cards" not in message for _idx, message in sent)


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
    assert result.pot == 40_000
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
            assert writers[0].writes == [b"name"]
            assert writers[1].writes == [b"name"]
        finally:
            manager._match_task.cancel()
            try:
                await manager._match_task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
