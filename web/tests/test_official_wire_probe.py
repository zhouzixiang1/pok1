import asyncio
from pathlib import Path
import importlib.util
import json
import pytest

from official_wire_probe import (
    OfficialWireReplay,
    TcpWireProbe,
    WireEventRecorder,
    replay_events,
    split_client_messages,
    split_server_messages,
)
from national_native import NATIVE_BOT_TEMPLATE
from sever.server.protocol import split_server_messages as split_transport_messages


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


def _decode_transport_chunks(chunks):
    messages = []
    remainder = ""
    for chunk in chunks:
        emitted, remainder = split_transport_messages(
            remainder + chunk,
            flush_boundary=False,
        )
        messages.extend(emitted)
    emitted, remainder = split_transport_messages(
        remainder,
        flush_boundary=True,
    )
    return messages + emitted, remainder


def _decode_probe_chunks(chunks):
    messages = []
    remainder = ""
    for chunk in chunks:
        emitted, remainder = split_server_messages(
            remainder + chunk,
            flush_numeric=False,
        )
        messages.extend(emitted)
    emitted, remainder = split_server_messages(
        remainder,
        flush_numeric=True,
    )
    return messages + emitted, remainder


def _candidate_decoder_class():
    namespace = {
        "__file__": str(ROOT / "synthetic-national-bot.py"),
        "__name__": "_synthetic_national_decoder_equivalence",
    }
    exec(compile(NATIVE_BOT_TEMPLATE, namespace["__file__"], "exec"), namespace)
    return namespace["NationalStreamDecoder"]


def _decode_candidate_chunks(decoder_class, chunks):
    decoder = decoder_class()
    messages = []
    for chunk in chunks:
        messages.extend(decoder.feed(chunk))
    messages.extend(decoder.flush_idle())
    return messages, decoder.buffer


def test_transport_candidate_and_official_probe_decoders_share_exact_corpus():
    decoder_class = _candidate_decoder_class()
    corpus = (
        (
            "earnChips -100preflop|SMALLBLIND|<0,3><1,12>raise 200call",
            [
                "earnChips -100",
                "preflop|SMALLBLIND|<0,3><1,12>",
                "raise 200",
                "call",
            ],
            "",
        ),
        ("raise 200", ["raise 200"], ""),
        ("earnChips -100", ["earnChips -100"], ""),
        ("earnChips\t-100", [], "earnChips\t-100"),
        ("earnChips  -100", [], "earnChips  -100"),
        ("earnChips\n-100", [], "earnChips\n-100"),
        ("raise  200", [], "raise  200"),
        ("raise ", [], "raise "),
        ("preflop|SMALLBLIND|<0,3>", [], "preflop|SMALLBLIND|<0,3>"),
        ("call\n", ["call"], "\n"),
    )
    for raw, expected_messages, expected_remainder in corpus:
        chunkings = [(raw,), tuple(raw)]
        chunkings.extend((raw[:cut], raw[cut:]) for cut in range(len(raw) + 1))
        for chunks in chunkings:
            expected = (expected_messages, expected_remainder)
            assert _decode_transport_chunks(chunks) == expected
            assert _decode_probe_chunks(chunks) == expected
            assert _decode_candidate_chunks(decoder_class, chunks) == expected


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
    assert summary["seats"]["A"]["pot"] == 40_000
    assert summary["seats"]["A"]["player_chips"] == 0
    assert summary["seats"]["A"]["opponent_chips"] == 0


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


def test_replay_applies_omitted_paid_closer_before_clearing_street():
    summary = replay_events([
        _event(0, "A", "server_to_bot", ["preflop|SMALLBLIND|<0,0><1,1>"]),
        _event(1, "A", "bot_to_server", ["raise 200"]),
        _event(2, "A", "server_to_bot", ["flop|<0,4><1,5><2,6>"]),
    ])

    seat = summary["seats"]["A"]
    assert summary["issues"] == []
    assert seat["pot"] == 400
    assert seat["player_chips"] == 19800
    assert seat["opponent_chips"] == 19800
    assert seat["player_bet"] == seat["opponent_bet"] == 0
    assert seat["hand_actions"][-1] == {
        "hand": 1,
        "stage": "preflop",
        "actor": "opponent",
        "action_type": "call",
        "committed": 100,
        "player_bet_after": 200,
        "opponent_bet_after": 200,
        "player_chips_after": 19800,
        "opponent_chips_after": 19800,
        "pot_after": 400,
        "inferred": True,
        "inference_boundary": "street:flop",
    }


def test_replay_does_not_duplicate_relayed_street_closer():
    summary = replay_events([
        _event(0, "A", "server_to_bot", ["preflop|SMALLBLIND|<0,0><1,1>"]),
        _event(1, "A", "bot_to_server", ["raise 200"]),
        _event(2, "A", "server_to_bot", ["call"]),
        _event(3, "A", "server_to_bot", ["flop|<0,4><1,5><2,6>"]),
    ])

    actions = summary["seats"]["A"]["hand_actions"]
    assert [item["action_type"] for item in actions] == ["raise", "call"]
    assert actions[-1]["inferred"] is False
    assert summary["seats"]["A"]["pot"] == 400
    assert summary["issues"] == []


def _showdown_events(*, a_reveal="<2,2><3,3>"):
    board = ["flop|<0,4><1,5><2,6>", "turn|<3,7>", "river|<0,8>"]
    return [
        _event(0, "A", "server_to_bot", ["preflop|SMALLBLIND|<0,0><1,1>"]),
        _event(0.1, "B", "server_to_bot", ["preflop|BIGBLIND|<2,2><3,3>"]),
        _event(1, "A", "bot_to_server", ["allin"]),
        _event(1.1, "B", "server_to_bot", ["allin"]),
        _event(1.2, "B", "bot_to_server", ["call"]),
        _event(2, "A", "server_to_bot", board),
        _event(2.1, "B", "server_to_bot", board),
        _event(3, "A", "server_to_bot", ["earnChips 20000"]),
        _event(3.1, "B", "server_to_bot", ["earnChips -20000"]),
        _event(4, "A", "server_to_bot", [f"oppo_hands|{a_reveal}"]),
        _event(4.1, "B", "server_to_bot", ["oppo_hands|<0,0><1,1>"]),
    ]


def _cross_seat_board_events(*, b_overrides=None, a_hole=None, b_hole=None):
    board_a = {
        "flop": "flop|<0,4><1,5><2,6>",
        "turn": "turn|<3,7>",
        "river": "river|<0,8>",
    }
    board_b = dict(board_a)
    board_b.update(b_overrides or {})
    events = [
        _event(
            0,
            "A",
            "server_to_bot",
            [f"preflop|SMALLBLIND|{a_hole or '<0,0><1,1>'}"],
        ),
        _event(
            0.1,
            "B",
            "server_to_bot",
            [f"preflop|BIGBLIND|{b_hole or '<2,2><3,3>'}"],
        ),
    ]
    timestamp = 1.0
    for stage in ("flop", "turn", "river"):
        events.append(
            _event(timestamp, "A", "server_to_bot", [board_a[stage]])
        )
        events.append(
            _event(timestamp + 0.1, "B", "server_to_bot", [board_b[stage]])
        )
        timestamp += 1.0
    return events


def _two_hand_blind_events(*, second_a="BIGBLIND", second_b="SMALLBLIND"):
    return [
        _event(0, "A", "server_to_bot", [
            "preflop|SMALLBLIND|<0,0><1,1>"
        ]),
        _event(0.1, "B", "server_to_bot", [
            "preflop|BIGBLIND|<2,2><3,3>"
        ]),
        _event(1, "A", "server_to_bot", [
            f"preflop|{second_a}|<0,4><1,5>"
        ]),
        _event(1.1, "B", "server_to_bot", [
            f"preflop|{second_b}|<2,6><3,7>"
        ]),
    ]


def test_replay_binds_complementary_blinds_and_accepts_normal_alternation():
    summary = replay_events(_two_hand_blind_events(), now=2.0)

    assert summary["issues"] == []
    assert summary["seats"]["A"]["blind_records"] == [
        {"hand": 1, "blind": "SMALLBLIND"},
        {"hand": 2, "blind": "BIGBLIND"},
    ]
    assert summary["seats"]["B"]["blind_records"] == [
        {"hand": 1, "blind": "BIGBLIND"},
        {"hand": 2, "blind": "SMALLBLIND"},
    ]


@pytest.mark.parametrize("blind", ["SMALLBLIND", "BIGBLIND"])
def test_replay_rejects_same_blind_on_both_connections(blind):
    summary = replay_events([
        _event(0, "A", "server_to_bot", [
            f"preflop|{blind}|<0,0><1,1>"
        ]),
        _event(0.1, "B", "server_to_bot", [
            f"preflop|{blind}|<2,2><3,3>"
        ]),
    ], now=1.0)

    mismatch = next(
        issue
        for issue in summary["issues"]
        if issue["kind"] == "blind_cross_seat_mismatch"
    )
    assert mismatch["hand"] == 1
    assert mismatch["blind_bindings"] == {"A": blind, "B": blind}


def test_replay_rejects_complementary_roles_that_fail_to_alternate():
    summary = replay_events(
        _two_hand_blind_events(
            second_a="SMALLBLIND",
            second_b="BIGBLIND",
        ),
        now=2.0,
    )

    alternation = [
        issue
        for issue in summary["issues"]
        if issue["kind"] == "blind_not_alternating"
    ]
    assert {issue["conn"] for issue in alternation} == {"A", "B"}
    assert all(issue["hand"] == 2 for issue in alternation)
    assert not any(
        issue["kind"] == "blind_cross_seat_mismatch"
        for issue in summary["issues"]
    )


def test_replay_binds_each_public_street_exactly_across_connections():
    summary = replay_events(_cross_seat_board_events(), now=4.0)

    assert summary["issues"] == []
    expected = [{
        "hand": 1,
        "streets": {
            "flop": [[0, 4], [1, 5], [2, 6]],
            "river": [[0, 8]],
            "turn": [[3, 7]],
        },
    }]
    assert summary["seats"]["A"]["public_card_records"] == expected
    assert summary["seats"]["B"]["public_card_records"] == expected


@pytest.mark.parametrize(
    "stage,replacement",
    [
        ("flop", "flop|<0,4><1,5><3,6>"),
        ("turn", "turn|<2,7>"),
        ("river", "river|<1,8>"),
    ],
)
def test_replay_rejects_cross_seat_public_card_mismatch_by_street(
    stage,
    replacement,
):
    summary = replay_events(
        _cross_seat_board_events(b_overrides={stage: replacement}),
        now=4.0,
    )

    mismatches = [
        issue
        for issue in summary["issues"]
        if issue["kind"] == "public_cards_cross_seat_mismatch"
    ]
    assert len(mismatches) == 1
    assert mismatches[0]["hand"] == 1
    assert mismatches[0]["board_stage"] == stage
    assert mismatches[0]["conn"] == "B"
    assert mismatches[0]["peer_conn"] == "A"


def test_replay_rejects_flop_order_drift_even_when_card_set_matches():
    summary = replay_events(
        _cross_seat_board_events(
            b_overrides={"flop": "flop|<1,5><0,4><2,6>"}
        ),
        now=4.0,
    )

    mismatch = next(
        issue
        for issue in summary["issues"]
        if issue["kind"] == "public_cards_cross_seat_mismatch"
    )
    assert mismatch["board_stage"] == "flop"
    assert mismatch["observed_cards"] == [[1, 5], [0, 4], [2, 6]]
    assert mismatch["peer_cards"] == [[0, 4], [1, 5], [2, 6]]


def test_replay_rejects_cross_seat_hole_collision():
    summary = replay_events([
        _event(0, "A", "server_to_bot", [
            "preflop|SMALLBLIND|<0,0><1,1>"
        ]),
        _event(0.1, "B", "server_to_bot", [
            "preflop|BIGBLIND|<0,0><3,3>"
        ]),
    ], now=1.0)

    collision = next(
        issue
        for issue in summary["issues"]
        if issue["kind"] == "cross_seat_hole_collision"
    )
    assert collision["hand"] == 1
    assert collision["left_conn"] == "A"
    assert collision["right_conn"] == "B"
    assert collision["collision"] == [[0, 0]]


@pytest.mark.parametrize(
    "stage,b_hole",
    [
        ("flop", "<0,4><3,3>"),
        ("turn", "<3,7><3,3>"),
        ("river", "<0,8><3,3>"),
    ],
)
def test_replay_rejects_peer_hole_collision_with_each_board_street(
    stage,
    b_hole,
):
    events = _cross_seat_board_events(b_hole=b_hole)
    # One A-side observation is sufficient to prove the peer-hole collision;
    # remove B's board messages so a same-wire collision cannot mask the gap.
    events = [
        event
        for event in events
        if not (
            event["conn"] == "B"
            and (event["messages"][0].startswith(("flop|", "turn|", "river|")))
        )
    ]
    summary = replay_events(events, now=4.0)

    collisions = [
        issue
        for issue in summary["issues"]
        if issue["kind"] == "cross_seat_hole_board_collision"
    ]
    assert len(collisions) == 1
    assert collisions[0]["hole_conn"] == "B"
    assert collisions[0]["board_conn"] == "A"
    assert collisions[0]["board_stage"] == stage
    assert not any(
        issue["kind"] == "public_cards_collision"
        for issue in summary["issues"]
    )


def test_replay_checks_a_hole_against_board_observed_only_on_b_wire():
    events = _cross_seat_board_events(a_hole="<0,4><1,1>")
    events = [
        event
        for event in events
        if not (
            event["conn"] == "A"
            and event["messages"][0].startswith(("flop|", "turn|", "river|"))
        )
    ]
    summary = replay_events(events, now=4.0)

    collision = next(
        issue
        for issue in summary["issues"]
        if issue["kind"] == "cross_seat_hole_board_collision"
    )
    assert collision["hole_conn"] == "A"
    assert collision["board_conn"] == "B"
    assert collision["board_stage"] == "flop"


def test_replay_binds_showdown_cards_to_the_other_connection_hole_cards():
    summary = replay_events(_showdown_events())

    assert summary["issues"] == []
    assert summary["seats"]["A"]["showdown_records"] == [{
        "hand": 1,
        "opponent_cards": [[2, 2], [3, 3]],
    }]
    assert summary["seats"]["B"]["showdown_records"] == [{
        "hand": 1,
        "opponent_cards": [[0, 0], [1, 1]],
    }]


def test_replay_rejects_showdown_cross_seat_mismatch_and_non_showdown_reveal():
    mismatch = replay_events(_showdown_events(a_reveal="<2,2><3,4>"))
    premature = replay_events([
        _event(0, "A", "server_to_bot", ["preflop|SMALLBLIND|<0,0><1,1>"]),
        _event(0.1, "B", "server_to_bot", ["preflop|BIGBLIND|<2,2><3,3>"]),
        _event(1, "A", "server_to_bot", ["oppo_hands|<2,2><3,3>"]),
    ])

    assert any(
        issue["kind"] == "showdown_cross_seat_hole_mismatch"
        for issue in mismatch["issues"]
    )
    assert any(
        issue["kind"] == "showdown_boundary_invalid"
        for issue in premature["issues"]
    )


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
