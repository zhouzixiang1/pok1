import asyncio
from pathlib import Path
import importlib.util
import json

from official_wire_probe import (
    OfficialWireReplay,
    TcpWireProbe,
    WireEventRecorder,
    replay_events,
    split_client_messages,
    split_server_messages,
)


ROOT = Path(__file__).resolve().parents[2]
ORACLE_FIXTURE = ROOT / "sever" / "tests" / "fixtures" / "official_raise_boundary_oracle_20260711.json"
TERMINAL_ORACLE_FIXTURE = (
    ROOT
    / "sever"
    / "tests"
    / "fixtures"
    / "official_terminal_settlement_oracle_20260711.json"
)


def _event(t, conn, direction, messages):
    return {
        "t": float(t),
        "dt": float(t),
        "conn": conn,
        "direction": direction,
        "messages": list(messages),
    }


def test_server_stream_split_handles_official_sticky_packets():
    messages, rest = split_server_messages(
        "earnChips -100preflop|SMALLBLIND|<0,3><1,12>raise 200call"
    )

    assert messages == [
        "earnChips -100",
        "preflop|SMALLBLIND|<0,3><1,12>",
        "raise 200",
        "call",
    ]
    assert rest == ""


def test_client_stream_split_preserves_illegal_raise_spacing_for_replay():
    messages, rest = split_client_messages("raise  200", allow_name=False)

    # At the idle boundary the complete malformed decision is surfaced
    # verbatim for illegality classification; it is never normalized.
    assert messages == ["raise  200"]
    assert rest == ""


def test_client_stream_split_defers_fragmented_numeric_tail_until_idle_flush():
    messages, rest = split_client_messages(
        "raise 2",
        allow_name=False,
        flush_numeric=False,
    )
    assert messages == []
    assert rest == "raise 2"

    messages, rest = split_client_messages(
        rest + "00",
        allow_name=False,
        flush_numeric=False,
    )
    assert messages == []
    assert rest == "raise 200"

    messages, rest = split_client_messages(
        rest,
        allow_name=False,
        flush_numeric=True,
    )
    assert messages == ["raise 200"]
    assert rest == ""


def test_wire_probe_incrementally_decodes_fragmented_utf8_name(tmp_path):
    class Reader:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        async def read(self, _size):
            return self.chunks.pop(0) if self.chunks else b""

    class Writer:
        def __init__(self):
            self.payload = bytearray()

        def write(self, payload):
            self.payload.extend(payload)

        async def drain(self):
            return None

    encoded = "底牌码农".encode("utf-8")
    reader = Reader([encoded[:1], encoded[1:4], encoded[4:]])
    writer = Writer()
    recorder = WireEventRecorder(tmp_path / "wire.jsonl")
    probe = TcpWireProbe(platform_host="127.0.0.1", platform_port=1, recorder=recorder)
    try:
        asyncio.run(probe._pipe("A", "bot_to_server", reader, writer))
    finally:
        recorder.close()

    assert bytes(writer.payload) == encoded
    parsed = [message for event in recorder.events for message in event["messages"]]
    assert parsed == ["底牌码农"]
    assert "\ufffd" not in json.dumps(recorder.events, ensure_ascii=False)


def test_replay_rejects_newline_as_team_name_framing():
    summary = replay_events([
        _event(0, "A", "server_to_bot", ["name"]),
        _event(1, "A", "bot_to_server", ["BotA\n"]),
    ])

    assert summary["issues"][0]["kind"] == "wire_name_format"


def test_replay_flags_second_postflop_check_as_illegal():
    summary = replay_events([
        _event(0, "B", "server_to_bot", ["flop|<0,1><1,2><2,3>"]),
        _event(1, "B", "server_to_bot", ["check"]),
        _event(2, "B", "bot_to_server", ["check"]),
    ])

    assert summary["issues"][0]["kind"] == "illegal_check"
    assert "postflop check is illegal" in summary["issues"][0]["reason"]


def test_replay_flags_allin_after_opponent_allin_as_illegal():
    summary = replay_events([
        _event(0, "A", "server_to_bot", ["preflop|BIGBLIND|<0,1><1,2>"]),
        _event(1, "A", "server_to_bot", ["allin"]),
        _event(2, "A", "bot_to_server", ["allin"]),
    ])

    assert summary["issues"][0]["kind"] == "illegal_allin"
    assert "after an allin" in summary["issues"][0]["reason"]


def test_replay_accepts_real_official_exact_2x_oracle_fixture():
    oracle = json.loads(ORACLE_FIXTURE.read_text(encoding="utf-8"))

    summary = replay_events(oracle["replay_events"])

    assert oracle["exact_2x_raw_wire_sha256"] == (
        "dc9dffa1121bee77bab1478842b7f336e1d4a72686e2ad7cbf322ed077bf85f3"
    )
    assert summary["issues"] == []
    assert summary["hands_started_min"] == 2
    assert summary["settlements_min"] == 2


def test_terminal_settlement_oracle_fixture_binds_wire_and_thp_prefixes():
    oracle = json.loads(TERMINAL_ORACLE_FIXTURE.read_text(encoding="utf-8"))

    assert oracle["exe_sha256"] == (
        "9d01b443d4920a7e06a487d87ea1b050ea2ca5359023602f98c3c236c734e81a"
    )
    assert oracle["wire_events_sha256"] == (
        "ca6e29cee830740ab511f06a3231df39edde26229529fc91bcc8a1c4a482d234"
    )
    assert oracle["thp_sha256"] == (
        "c70b60ac80375a2bf41fa72825bd91358cc48c5369eaee778aec0dd10226ca50"
    )
    assert oracle["wire_hands_started"] == 70
    assert oracle["wire_settlement_hands"] == {
        "first": 1,
        "last": 69,
        "count": 69,
        "gaps": [],
        "duplicates": [],
    }
    assert oracle["thp_state_indices"]["count"] == 70
    assert oracle["wire_prefix_digest"] == oracle["thp_prefix_digest"]
    assert oracle["wire_prefix_digest"] == (
        "b5079bf195205a2c90e1aeb8a9fcb28a35efe922ef12392b945212590c6acd07"
    )
    assert oracle["terminal_state"] == "STATE:69:f:Jc9s|3cTs:50|-50:BotB|BotA;"
    assert oracle["wire_totals_before_terminal"] == {"BotA": 19721, "BotB": -19721}
    assert oracle["terminal_earnings"] == {"BotA": -50, "BotB": 50}
    assert oracle["thp_match_totals"] == {"BotA": 19671, "BotB": -19671}
    assert oracle["thp_footer_result"] == "BotA赢得19671个筹码"
    assert sum(oracle["terminal_earnings"].values()) == 0
    assert sum(oracle["thp_match_totals"].values()) == 0
    assert oracle["strength_evaluation"] == "not_applicable"


def test_replay_rejects_reraise_below_inclusive_2x_boundary():
    summary = replay_events([
        _event(0, "B", "server_to_bot", ["preflop|BIGBLIND|<0,1><1,2>"]),
        _event(1, "B", "server_to_bot", ["raise 200"]),
        _event(2, "B", "bot_to_server", ["raise 399"]),
    ])

    issue = next(item for item in summary["issues"] if item["kind"] == "illegal_raise")
    assert "at least 2x" in issue["reason"]


def test_replay_flags_platform_silent_settlement_gap_without_pending_bot_timeout():
    summary = replay_events([
        _event(0, "A", "server_to_bot", ["river|<0,1>"]),
        _event(1, "A", "bot_to_server", ["raise 100"]),
        _event(2, "A", "server_to_bot", ["call"]),
        _event(64, "A", "server_to_bot", ["earnChips 200"]),
    ])

    assert summary["issues"][0]["kind"] == "platform_silent_idle_gap"
    assert summary["issues"][0]["waited_sec"] >= 62
    assert summary["max_platform_silent_gap_sec"] >= 62
    assert summary["pending_expected_actions"] == []


def test_replay_does_not_create_runout_actions_after_allin_is_called():
    summary = replay_events([
        _event(0, "A", "server_to_bot", ["preflop|BIGBLIND|<0,1><1,2>"]),
        _event(1, "A", "server_to_bot", ["allin"]),
        _event(2, "A", "bot_to_server", ["call"]),
        _event(3, "A", "server_to_bot", ["flop|<0,3><1,4><2,5>"]),
        _event(4, "A", "server_to_bot", ["turn|<3,6>"]),
        _event(5, "A", "server_to_bot", ["river|<0,7>"]),
    ])

    assert summary["pending_expected_actions"] == []
    assert not any(item["kind"] == "pending_bot_response_timeout" for item in summary["issues"])


def test_replay_reports_unparseable_client_tail_at_eof():
    event = _event(0, "A", "bot_to_server", [])
    event.update({
        "event_type": "stream_eof",
        "remaining": "raise  200",
    })

    summary = replay_events([event])

    assert summary["issues"][0]["kind"] == "wire_stream_eof_remainder"
    assert summary["issues"][0]["direction"] == "bot_to_server"


def test_replay_flags_client_action_without_pending_request():
    summary = replay_events([
        _event(0, "A", "server_to_bot", ["preflop|SMALLBLIND|<0,1><1,2>"]),
        _event(1, "A", "bot_to_server", ["call"]),
        _event(2, "A", "bot_to_server", ["call"]),
    ])

    assert summary["issues"][0]["kind"] == "unsolicited_client_action"
    assert summary["issues"][0]["message"] == "call"
    assert summary["warnings"] == []


def test_replay_reports_real_pending_bot_response_timeout():
    replay = OfficialWireReplay()
    replay.consume_event(_event(0, "A", "server_to_bot", ["flop|<0,1><1,2><2,3>"]))

    summary = replay.summary(now=61)

    assert summary["issues"][0]["kind"] == "pending_bot_response_timeout"
    assert summary["pending_expected_actions"][0]["expected_reason"] == "flop_first_action"


def test_replay_records_settlement_hand_ids_and_signed_amounts():
    summary = replay_events([
        _event(0, "A", "server_to_bot", ["preflop|BIGBLIND|<0,1><1,2>"]),
        _event(1, "A", "server_to_bot", ["earnChips -50"]),
        _event(2, "A", "server_to_bot", ["preflop|SMALLBLIND|<0,3><1,4>"]),
        _event(3, "A", "server_to_bot", ["earnChips 100"]),
    ])

    assert summary["seats"]["A"]["settlement_records"] == [
        {"hand": 1, "amount": -50},
        {"hand": 2, "amount": 100},
    ]
    assert summary["issues"] == []


def test_replay_rejects_duplicate_and_gapped_settlement_hands():
    duplicate = replay_events([
        _event(0, "A", "server_to_bot", ["preflop|BIGBLIND|<0,1><1,2>"]),
        _event(1, "A", "server_to_bot", ["earnChips -50"]),
        _event(2, "A", "server_to_bot", ["earnChips -50"]),
    ])
    gap = replay_events([
        _event(0, "A", "server_to_bot", ["preflop|BIGBLIND|<0,1><1,2>"]),
        _event(1, "A", "server_to_bot", ["preflop|SMALLBLIND|<0,3><1,4>"]),
        _event(2, "A", "server_to_bot", ["earnChips 50"]),
    ])

    assert {item["kind"] for item in duplicate["issues"]} == {
        "settlement_hand_sequence",
        "duplicate_settlement",
    }
    assert any(item["kind"] == "settlement_hand_sequence" for item in gap["issues"])


def test_scripted_diagnostic_client_defers_fragmented_numeric_message():
    spec = importlib.util.spec_from_file_location(
        "official_scripted_bot_cli",
        ROOT / "scripts" / "official_scripted_bot.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class Wire:
        def __init__(self):
            self.sent = []

        def sendall(self, payload):
            self.sent.append(payload)

    wire = Wire()
    client = module.ScriptedClient(
        scenario=module.CHECK_CALL_DOWN,
        name="Diagnostic",
        log_path=None,
        action_delay=0.0,
    )
    try:
        client.dispatch_raw(wire, b"raise 2")
        client.dispatch_raw(wire, b"00")
        assert wire.sent == []
        assert client.buffer == "raise 200"
        client.flush_idle(wire)
        assert wire.sent == [b"call"]
        assert client.buffer == ""
    finally:
        client.close()


def test_wire_probe_target_reached_requires_no_pending_action():
    spec = importlib.util.spec_from_file_location("official_wire_probe_cli", ROOT / "scripts" / "official_wire_probe.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert not module._target_reached({
        "hands_started_min": 1,
        "settlements_min": 0,
        "pending_expected_actions": [{"conn": "A"}],
    }, 1)
    assert not module._target_reached({
        "hands_started_min": 1,
        "settlements_min": 0,
        "pending_expected_actions": [],
    }, 1)
    assert module._target_reached({
        "hands_started_min": 1,
        "settlements_min": 1,
        "pending_expected_actions": [],
    }, 1)
    assert not module._target_reached({
        "hands_started_min": 70,
        "settlements_min": 69,
        "pending_expected_actions": [],
    }, 70)
