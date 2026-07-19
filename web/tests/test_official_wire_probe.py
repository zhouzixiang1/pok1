import asyncio
import hashlib
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
ALLIN_RUNOUT_ORACLE_FIXTURE = (
    ROOT
    / "sever"
    / "tests"
    / "fixtures"
    / "official_allin_runout_wire_oracle_20260719.json"
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


def test_wire_probe_replays_idle_action_at_last_raw_observation(tmp_path):
    class Reader:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def read(self, _size):
            return await self.queue.get()

    class Writer:
        def __init__(self, recorder):
            self.payload = bytearray()
            self.recorder = recorder
            self.responded = False

        def write(self, payload):
            self.payload.extend(payload)

        async def drain(self):
            if not self.responded:
                self.responded = True
                self.recorder.record(
                    conn="A",
                    direction="server_to_bot",
                    raw=b"flop|<0,3><1,4><2,5>",
                    messages=["flop|<0,3><1,4><2,5>"],
                    remaining="",
                )

    async def exercise(probe, reader):
        await reader.queue.put(b"call")

        async def close_after_idle():
            await asyncio.sleep(0.07)
            await reader.queue.put(b"")

        closer = asyncio.create_task(close_after_idle())
        await probe._pipe("A", "bot_to_server", reader, writer)
        await closer

    recorder = WireEventRecorder(tmp_path / "wire.jsonl")
    recorder.record(
        conn="A",
        direction="server_to_bot",
        raw=b"preflop|SMALLBLIND|<0,1><1,2>",
        messages=["preflop|SMALLBLIND|<0,1><1,2>"],
        remaining="",
    )
    probe = TcpWireProbe(
        platform_host="127.0.0.1",
        platform_port=1,
        recorder=recorder,
    )
    probe._awaiting_name["A"] = False
    reader = Reader()
    writer = Writer(recorder)
    try:
        asyncio.run(exercise(probe, reader))
    finally:
        recorder.close()

    assert bytes(writer.payload) == b"call"
    raw_index = next(
        index
        for index, event in enumerate(recorder.events)
        if event["raw_hex"] == b"call".hex()
        and event["direction"] == "bot_to_server"
    )
    flop_index = next(
        index
        for index, event in enumerate(recorder.events)
        if event["messages"] == ["flop|<0,3><1,4><2,5>"]
    )
    flush_index = next(
        index
        for index, event in enumerate(recorder.events)
        if event["event_type"] == "idle_flush"
    )
    assert raw_index < flop_index < flush_index
    assert (
        recorder.events[flush_index]["observation_seq"]
        == recorder.events[raw_index]["observation_seq"]
    )
    live_prefix = replay_events(
        recorder.events[: flop_index + 1],
        finalized=False,
    )
    assert not any(
        issue["kind"] == "street_boundary_unproved"
        for issue in live_prefix["issues"]
    )
    assert any(
        warning["kind"] == "provisional_street_boundary_unproved"
        and warning["strict_issue_kind"] == "street_boundary_unproved"
        for warning in live_prefix["warnings"]
    )
    unresolved_finalized = json.loads(json.dumps(
        recorder.events[: flop_index + 1]
    ))
    epoch = float(unresolved_finalized[0]["t"]) - float(
        unresolved_finalized[0]["dt"]
    )
    marker_t = max(
        float(unresolved_finalized[-1]["t"]),
        float(unresolved_finalized[-1]["observation_t"]),
    ) + 0.001
    unresolved_finalized.append({
        "ts": unresolved_finalized[-1]["ts"],
        "t": marker_t,
        "dt": marker_t - epoch,
        "causal_order_schema_version": 1,
        "record_seq": len(unresolved_finalized) + 1,
        "observation_seq": max(
            event["observation_seq"] for event in unresolved_finalized
        ) + 1,
        "observation_t": marker_t,
        "observation_dt": marker_t - epoch,
        "conn": "*",
        "direction": "probe_lifecycle",
        "event_type": "capture_finalized",
        "raw_repr": "",
        "raw_hex": "",
        "messages": [],
        "remaining": "",
        "details": {},
    })
    unresolved_summary = replay_events(
        unresolved_finalized,
        finalized=True,
    )
    assert any(
        issue["kind"] == "wire_event_causal_order_invalid"
        and issue["reason"] == "causal_wire_event_pending_buffer_unresolved"
        for issue in unresolved_summary["issues"]
    )
    assert not any(
        warning["kind"] == "provisional_street_boundary_unproved"
        for warning in replay_events(recorder.events)["warnings"]
    )
    assert replay_events(recorder.events)["issues"] == []

    causal_fields = {
        "causal_order_schema_version",
        "record_seq",
        "observation_seq",
        "observation_t",
        "observation_dt",
    }
    legacy_append_order = [
        {key: value for key, value in event.items() if key not in causal_fields}
        for event in recorder.events
    ]
    assert any(
        issue["kind"] == "unsolicited_client_action"
        for issue in replay_events(legacy_append_order)["issues"]
    )


def test_deferred_invalid_action_becomes_strict_after_idle_flush(tmp_path):
    class Reader:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def read(self, _size):
            return await self.queue.get()

    class Writer:
        def __init__(self, recorder):
            self.recorder = recorder
            self.drains = 0

        def write(self, _payload):
            return None

        async def drain(self):
            self.drains += 1
            message = (
                "flop|<0,3><1,4><2,5>"
                if self.drains == 1
                else "turn|<3,6>"
            )
            self.recorder.record(
                conn="A",
                direction="server_to_bot",
                raw=message.encode("utf-8"),
                messages=[message],
                remaining="",
            )

    async def exercise(probe, reader, writer):
        async def feed():
            await reader.queue.put(b"call")
            await asyncio.sleep(0.07)
            await reader.queue.put(b"call")
            await asyncio.sleep(0.07)
            await reader.queue.put(b"")

        feeder = asyncio.create_task(feed())
        await probe._pipe("A", "bot_to_server", reader, writer)
        await feeder

    recorder = WireEventRecorder(tmp_path / "wire.jsonl")
    recorder.record(
        conn="A",
        direction="server_to_bot",
        raw=b"preflop|SMALLBLIND|<0,1><1,2>",
        messages=["preflop|SMALLBLIND|<0,1><1,2>"],
        remaining="",
    )
    probe = TcpWireProbe(
        platform_host="127.0.0.1",
        platform_port=1,
        recorder=recorder,
    )
    probe._awaiting_name["A"] = False
    try:
        asyncio.run(exercise(probe, Reader(), Writer(recorder)))
    finally:
        recorder.close()

    turn_index = next(
        index
        for index, event in enumerate(recorder.events)
        if event["messages"] == ["turn|<3,6>"]
    )
    second_flush_index = [
        index
        for index, event in enumerate(recorder.events)
        if event["event_type"] == "idle_flush"
    ][1]
    prefix = recorder.events[: turn_index + 1]
    live = replay_events(
        prefix,
        now=max(float(event["observation_t"]) for event in prefix),
    )
    assert live["issues"] == []
    assert any(
        warning["kind"] == "provisional_street_boundary_unproved"
        for warning in live["warnings"]
    )
    flushed = replay_events(
        recorder.events[: second_flush_index + 1],
        now=float(recorder.events[second_flush_index]["t"]),
    )
    assert any(
        issue["kind"] in {"illegal_call", "unsolicited_client_action"}
        for issue in flushed["issues"]
    )
    assert not any(
        warning["kind"] == "provisional_street_boundary_unproved"
        for warning in flushed["warnings"]
    )


def test_pending_client_buffer_does_not_hide_older_same_connection_boundary(tmp_path):
    recorder = WireEventRecorder(tmp_path / "wire.jsonl")
    try:
        for direction, raw, messages, remaining in (
            (
                "server_to_bot",
                b"preflop|SMALLBLIND|<0,1><1,2>",
                ["preflop|SMALLBLIND|<0,1><1,2>"],
                "",
            ),
            (
                "server_to_bot",
                b"flop|<0,3><1,4><2,5>",
                ["flop|<0,3><1,4><2,5>"],
                "",
            ),
            ("bot_to_server", b"call", [], "call"),
            ("server_to_bot", b"turn|<3,6>", ["turn|<3,6>"], ""),
        ):
            recorder.record(
                conn="A",
                direction=direction,
                raw=raw,
                messages=messages,
                remaining=remaining,
            )
    finally:
        recorder.close()

    summary = replay_events(
        recorder.events,
        now=max(float(event["observation_t"]) for event in recorder.events),
    )
    strict_boundaries = [
        issue
        for issue in summary["issues"]
        if issue["kind"] == "street_boundary_unproved"
    ]
    provisional_boundaries = [
        warning
        for warning in summary["warnings"]
        if warning["kind"] == "provisional_street_boundary_unproved"
    ]
    assert [issue["message"] for issue in strict_boundaries] == [
        "flop|<0,3><1,4><2,5>"
    ]
    assert [warning["message"] for warning in provisional_boundaries] == [
        "turn|<3,6>"
    ]


@pytest.mark.parametrize("pending_text", ["garbage", "rai", "BotName"])
def test_noncanonical_pending_buffer_never_downgrades_live_boundary(
    tmp_path,
    pending_text,
):
    recorder = WireEventRecorder(tmp_path / "wire.jsonl")
    try:
        recorder.record(
            conn="A",
            direction="server_to_bot",
            raw=b"preflop|SMALLBLIND|<0,1><1,2>",
            messages=["preflop|SMALLBLIND|<0,1><1,2>"],
            remaining="",
        )
        source = recorder.record(
            conn="A",
            direction="bot_to_server",
            raw=b"call",
            messages=[],
            remaining="call",
        )
        recorder.record(
            conn="A",
            direction="bot_to_server",
            raw=b"",
            messages=["call"],
            remaining="",
            event_type="idle_flush",
            observation_seq=source["observation_seq"],
            observation_t=source["observation_t"],
            deferred_parser_mode="client_action",
        )
        recorder.record(
            conn="A",
            direction="server_to_bot",
            raw=b"flop|<0,3><1,4><2,5>",
            messages=["flop|<0,3><1,4><2,5>"],
            remaining="",
        )
        pending_messages, pending_remaining = split_client_messages(
            pending_text,
            allow_name=False,
            flush_numeric=False,
        )
        assert pending_messages == []
        assert pending_remaining == pending_text
        recorder.record(
            conn="A",
            direction="bot_to_server",
            raw=pending_text.encode("utf-8"),
            messages=[],
            remaining=pending_text,
        )
        recorder.record(
            conn="A",
            direction="server_to_bot",
            raw=b"turn|<3,6>",
            messages=["turn|<3,6>"],
            remaining="",
        )
    finally:
        recorder.close()

    summary = replay_events(
        recorder.events,
        now=max(float(event["observation_t"]) for event in recorder.events),
    )
    assert any(
        issue["kind"] == "street_boundary_unproved"
        and issue["message"] == "turn|<3,6>"
        for issue in summary["issues"]
    )
    assert not any(
        warning["kind"] == "provisional_street_boundary_unproved"
        for warning in summary["warnings"]
    )


def test_wire_probe_binds_fragmented_raise_to_last_contributing_chunk(tmp_path):
    class Reader:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def read(self, _size):
            return await self.queue.get()

    class Writer:
        def __init__(self, recorder):
            self.payload = bytearray()
            self.recorder = recorder
            self.responded = False

        def write(self, payload):
            self.payload.extend(payload)

        async def drain(self):
            if self.payload == b"raise 200" and not self.responded:
                self.responded = True
                self.recorder.record(
                    conn="A",
                    direction="server_to_bot",
                    raw=b"flop|<0,3><1,4><2,5>",
                    messages=["flop|<0,3><1,4><2,5>"],
                    remaining="",
                )

    async def exercise(probe, reader):
        await reader.queue.put(b"raise 2")
        await reader.queue.put(b"00")

        async def close_after_idle():
            await asyncio.sleep(0.07)
            await reader.queue.put(b"")

        closer = asyncio.create_task(close_after_idle())
        await probe._pipe("A", "bot_to_server", reader, writer)
        await closer

    recorder = WireEventRecorder(tmp_path / "wire.jsonl")
    recorder.record(
        conn="A",
        direction="server_to_bot",
        raw=b"preflop|SMALLBLIND|<0,1><1,2>",
        messages=["preflop|SMALLBLIND|<0,1><1,2>"],
        remaining="",
    )
    probe = TcpWireProbe(
        platform_host="127.0.0.1",
        platform_port=1,
        recorder=recorder,
    )
    probe._awaiting_name["A"] = False
    reader = Reader()
    writer = Writer(recorder)
    try:
        asyncio.run(exercise(probe, reader))
    finally:
        recorder.close()

    raw_events = [
        event
        for event in recorder.events
        if event["direction"] == "bot_to_server" and event["raw_hex"]
    ]
    flush = next(
        event for event in recorder.events if event["event_type"] == "idle_flush"
    )
    assert [event["messages"] for event in raw_events] == [[], []]
    assert flush["messages"] == ["raise 200"]
    assert flush["observation_seq"] == raw_events[-1]["observation_seq"]
    assert flush["observation_seq"] != raw_events[0]["observation_seq"]
    assert replay_events(recorder.events)["issues"] == []


def test_causal_replay_keeps_malformed_suffix_fail_closed(tmp_path):
    recorder = WireEventRecorder(tmp_path / "wire.jsonl")
    try:
        recorder.record(
            conn="A",
            direction="server_to_bot",
            raw=b"preflop|SMALLBLIND|<0,1><1,2>",
            messages=["preflop|SMALLBLIND|<0,1><1,2>"],
            remaining="",
        )
        raw = recorder.record(
            conn="A",
            direction="bot_to_server",
            raw=b"callx",
            messages=[],
            remaining="callx",
        )
        recorder.record(
            conn="A",
            direction="bot_to_server",
            raw=b"",
            messages=["callx"],
            remaining="",
            event_type="idle_flush",
            observation_seq=raw["observation_seq"],
            observation_t=raw["observation_t"],
            deferred_parser_mode="client_action",
        )
    finally:
        recorder.close()

    summary = replay_events(recorder.events)
    assert [issue["kind"] for issue in summary["issues"]] == [
        "wire_action_format"
    ]


def _causal_event(record_seq, observation_seq, t, conn, direction, messages, **extra):
    event = {
        "causal_order_schema_version": 1,
        "record_seq": record_seq,
        "observation_seq": observation_seq,
        "observation_t": float(t),
        "observation_dt": float(t),
        "t": float(extra.pop("recorded_t", t)),
        "dt": float(extra.pop("recorded_dt", t)),
        "conn": conn,
        "direction": direction,
        "event_type": extra.pop("event_type", "data"),
        "raw_hex": extra.pop("raw_hex", ""),
        "messages": list(messages),
        "remaining": extra.pop("remaining", ""),
        "details": {},
        **extra,
    }
    if event["event_type"] in {"idle_flush", "eof_flush"}:
        event.setdefault(
            "deferred_parser_mode",
            "server" if direction == "server_to_bot" else "client_action",
        )
    return event


def _causal_issue_reason(events, *, finalized=False):
    summary = replay_events(events, finalized=finalized)
    issue = next(
        (
            item
            for item in summary["issues"]
            if item["kind"] == "wire_event_causal_order_invalid"
        ),
        None,
    )
    return None if issue is None else issue.get("reason")


def _capture_finalized_event(record_seq, observation_seq, t):
    return _causal_event(
        record_seq,
        observation_seq,
        t,
        "*",
        "probe_lifecycle",
        [],
        event_type="capture_finalized",
    )


@pytest.mark.parametrize("schema", [True, 1.0])
def test_causal_schema_version_requires_exact_integer(schema):
    event = _causal_event(
        1,
        1,
        0,
        "A",
        "server_to_bot",
        ["name"],
        raw_hex=b"name".hex(),
    )
    event["causal_order_schema_version"] = schema

    assert _causal_issue_reason([event]) == "causal_wire_event_schema_invalid"


def test_causal_replay_rejects_duplicate_or_stale_boundary_proof():
    base = [
        _causal_event(
            1,
            1,
            0,
            "A",
            "server_to_bot",
            ["name"],
            raw_hex=b"name".hex(),
        ),
        _causal_event(
            2,
            2,
            1,
            "A",
            "bot_to_server",
            [],
            raw_hex=b"BotA".hex(),
            remaining="BotA",
        ),
    ]
    proof = _causal_event(
        3,
        2,
        1,
        "A",
        "bot_to_server",
        ["BotA"],
        event_type="idle_flush",
        recorded_t=1.1,
        recorded_dt=1.1,
        deferred_parser_mode="client_name",
    )
    duplicate = dict(proof, record_seq=4, t=1.2, dt=1.2)
    assert _causal_issue_reason([*base, proof, duplicate]) == (
        "causal_wire_event_observation_reuse_invalid"
    )

    latest = _causal_event(
        3,
        3,
        2,
        "A",
        "bot_to_server",
        [],
        raw_hex=b"x".hex(),
        remaining="BotAx",
    )
    stale = dict(proof, record_seq=4, t=2.1, dt=2.1)
    assert _causal_issue_reason([*base, latest, stale]) == (
        "causal_wire_event_observation_reuse_invalid"
    )


def test_causal_replay_rebuilds_data_transition_from_raw_bytes():
    events = [
        _causal_event(
            1,
            1,
            0,
            "A",
            "server_to_bot",
            ["preflop|SMALLBLIND|<0,1><1,2>"],
            raw_hex=b"preflop|SMALLBLIND|<0,1><1,2>".hex(),
        ),
        # The envelope lies about the raw suffix.  Deferred validation alone
        # would turn raw ``callx`` into a legal semantic ``call``.
        _causal_event(
            2,
            2,
            1,
            "A",
            "bot_to_server",
            [],
            raw_hex=b"callx".hex(),
            remaining="call",
        ),
        _causal_event(
            3,
            2,
            1,
            "A",
            "bot_to_server",
            ["call"],
            event_type="idle_flush",
            recorded_t=1.1,
            recorded_dt=1.1,
            deferred_parser_mode="client_action",
        ),
    ]

    assert _causal_issue_reason(events) == "causal_wire_data_parse_mismatch"


def test_causal_replay_rejects_terminal_remainder_mismatch():
    events = [
        _causal_event(
            1,
            1,
            0,
            "A",
            "server_to_bot",
            [],
            raw_hex=b"preflop|".hex(),
            remaining="preflop|",
        ),
        _causal_event(
            2,
            2,
            1,
            "A",
            "server_to_bot",
            [],
            event_type="stream_eof",
            remaining="",
        ),
    ]

    assert _causal_issue_reason(events) == (
        "causal_wire_event_terminal_remainder_mismatch"
    )


def test_finalized_causal_replay_requires_one_exact_last_marker():
    source = _causal_event(
        1,
        1,
        0,
        "A",
        "server_to_bot",
        ["name"],
        raw_hex=b"name".hex(),
    )
    marker = _capture_finalized_event(2, 2, 1)

    assert _causal_issue_reason([], finalized=True) == (
        "causal_wire_capture_finalized_missing"
    )
    assert _causal_issue_reason([source], finalized=True) == (
        "causal_wire_capture_finalized_missing"
    )
    assert _causal_issue_reason(
        [source, marker, _capture_finalized_event(3, 3, 2)],
        finalized=True,
    ) == "causal_wire_capture_finalized_duplicate"
    trailing = _causal_event(
        3,
        3,
        2,
        "A",
        "probe_lifecycle",
        [],
        event_type="upstream_connect_failed",
    )
    assert _causal_issue_reason([source, marker, trailing], finalized=True) == (
        "causal_wire_capture_finalized_invalid"
    )
    assert _causal_issue_reason([source, marker], finalized=True) is None


def test_finalized_causal_replay_rejects_pending_utf8_decoder_bytes():
    events = [
        _causal_event(
            1,
            1,
            0,
            "A",
            "server_to_bot",
            [],
            raw_hex=b"\xe4".hex(),
            remaining="",
        ),
        _capture_finalized_event(2, 2, 1),
    ]

    assert _causal_issue_reason(events, finalized=True) == (
        "causal_wire_event_pending_utf8_unresolved"
    )


def test_live_replay_waits_for_fragmented_utf8_before_finalization():
    encoded = "候选".encode("utf-8")
    events = [
        _causal_event(
            1,
            1,
            0,
            "A",
            "server_to_bot",
            ["name"],
            raw_hex=b"name".hex(),
        ),
        _causal_event(
            2,
            2,
            1,
            "A",
            "bot_to_server",
            [],
            raw_hex=encoded[:1].hex(),
            remaining="",
        ),
    ]
    assert _causal_issue_reason(events, finalized=False) is None

    events.extend([
        _causal_event(
            3,
            3,
            2,
            "A",
            "bot_to_server",
            [],
            raw_hex=encoded[1:].hex(),
            remaining="候选",
        ),
        _causal_event(
            4,
            3,
            2,
            "A",
            "bot_to_server",
            ["候选"],
            event_type="idle_flush",
            recorded_t=2.1,
            recorded_dt=2.1,
            deferred_parser_mode="client_name",
        ),
        _capture_finalized_event(5, 4, 3),
    ])
    assert _causal_issue_reason(events, finalized=True) is None


def test_capture_finalized_time_preserves_silent_request_timeout():
    events = [
        _causal_event(
            1,
            1,
            0,
            "A",
            "server_to_bot",
            ["preflop|SMALLBLIND|<0,1><1,2>"],
            raw_hex=b"preflop|SMALLBLIND|<0,1><1,2>".hex(),
        ),
        _capture_finalized_event(2, 2, 61),
    ]

    summary = replay_events(events, finalized=True)
    assert not any(
        issue["kind"] == "wire_event_causal_order_invalid"
        for issue in summary["issues"]
    )
    assert any(
        issue["kind"] == "pending_bot_response_timeout"
        for issue in summary["issues"]
    )


def test_delayed_boundary_proof_allows_load_but_source_record_lag_does_not():
    source = _causal_event(
        1,
        1,
        0,
        "A",
        "server_to_bot",
        ["preflop|SMALLBLIND|<0,1><1,2>"],
        raw_hex=b"preflop|SMALLBLIND|<0,1><1,2>".hex(),
    )
    raw_call = _causal_event(
        2,
        2,
        1,
        "A",
        "bot_to_server",
        [],
        raw_hex=b"call".hex(),
        remaining="call",
    )
    delayed_proof = _causal_event(
        3,
        2,
        1,
        "A",
        "bot_to_server",
        ["call"],
        event_type="idle_flush",
        recorded_t=3.1,
        recorded_dt=3.1,
        deferred_parser_mode="client_action",
    )
    assert _causal_issue_reason([source, raw_call, delayed_proof]) is None

    delayed_source = dict(source, t=2.0, dt=2.0)
    assert _causal_issue_reason([delayed_source]) == (
        "causal_wire_event_record_time_invalid"
    )


@pytest.mark.parametrize(
    ("last_byte_at", "expected_timeout"),
    ((59.999, False), (60.001, True)),
)
def test_causal_replay_timeout_uses_last_contributing_byte(
    last_byte_at,
    expected_timeout,
):
    events = [
        _causal_event(
            1,
            1,
            0,
            "A",
            "server_to_bot",
            ["preflop|SMALLBLIND|<0,1><1,2>"],
            raw_hex=b"preflop|SMALLBLIND|<0,1><1,2>".hex(),
        ),
        _causal_event(
            2,
            2,
            last_byte_at,
            "A",
            "bot_to_server",
            [],
            raw_hex=b"call".hex(),
            remaining="call",
        ),
        _causal_event(
            3,
            2,
            last_byte_at,
            "A",
            "bot_to_server",
            ["call"],
            event_type="idle_flush",
            recorded_t=last_byte_at + 0.05,
            recorded_dt=last_byte_at + 0.05,
        ),
    ]

    kinds = {issue["kind"] for issue in replay_events(events)["issues"]}
    assert ("pending_bot_response_timeout" in kinds) is expected_timeout


def test_causal_replay_rejects_mixed_or_tampered_event_metadata():
    causal = _causal_event(
        1,
        1,
        0,
        "A",
        "server_to_bot",
        ["name"],
        raw_hex=b"name".hex(),
    )
    legacy = _event(1, "A", "bot_to_server", ["BotA"])
    mixed = replay_events([causal, legacy])
    assert mixed["issues"][0]["kind"] == "wire_event_causal_order_invalid"
    assert mixed["issues"][0]["reason"] == "mixed_legacy_and_causal_wire_events"

    tampered = [dict(causal), dict(causal)]
    tampered[1].update({
        "record_seq": 2,
        "conn": "B",
        "direction": "bot_to_server",
        "event_type": "idle_flush",
        "messages": ["call"],
        "raw_hex": "",
    })
    rejected = replay_events(tampered)
    assert rejected["issues"][0]["kind"] == "wire_event_causal_order_invalid"
    assert rejected["issues"][0]["reason"] == "causal_wire_event_observation_reuse_invalid"


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


def test_called_allin_wire_oracle_fixture_is_exact_and_zero_strength():
    raw = ALLIN_RUNOUT_ORACLE_FIXTURE.read_bytes()
    oracle = json.loads(raw)

    assert hashlib.sha256(raw).hexdigest() == (
        "a81c804d1940437fb259d0119c7bc1b06e968fcd5f20eb4364ab3f594156ef48"
    )
    assert oracle["schema_version"] == 2
    assert oracle["authority_scope"] == "official_exe_wire_compliance_only"
    assert oracle["strength_weight"] == 0
    assert {
        (item["stage"], item["public_cards_observed"])
        for item in oracle["observations"]
    } == {("preflop", 0), ("flop", 3), ("turn", 4)}
    assert oracle["accepted_board_prefixes"] == {
        "preflop": 0,
        "flop": 3,
        "turn": 4,
    }
    assert {
        (item["stage"], item["public_cards_observed"], item["thp_public_cards"])
        for item in oracle["thp_prefix_observations"]
    } == {("flop", 3, 3), ("turn", 4, 4)}
    assert oracle["required_strict_thp_proof"][
        "allowed_public_board_shapes"
    ] == "exact_wire_prefix_or_complete_five"
    assert oracle["required_strict_thp_proof"]["bind_terminal_action"] is True
    assert len(oracle["live_deferred_action_observations"]) == 4
    assert all(
        item["live_issue"] == "street_boundary_unproved"
        and item["finalized_issues"] == []
        for item in oracle["live_deferred_action_observations"]
    )
    assert oracle["natural_hand_70"] == {
        "wire_settlement_expected": False,
        "dual_showdown_reveal_required_for_called_allin": True,
        "wire_boundary_is_provisional": True,
        "strict_thp_state_69_and_footer_required_for_certification": True,
    }


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


def _called_allin_without_runout_events(
    *,
    stage="preflop",
    include_a_settlement=True,
    include_b_settlement=True,
    include_call=True,
    fold_instead=False,
    reorder_peer_settlement=False,
    a_settlement_amount=20000,
    b_settlement_amount=-20000,
    malformed_prefix=False,
    duplicate_allin_response_on_a=False,
):
    response = "fold" if fold_instead else "call"
    a_blind = "SMALLBLIND" if stage == "preflop" else "BIGBLIND"
    b_blind = "BIGBLIND" if stage == "preflop" else "SMALLBLIND"
    events = [
        _event(0, "A", "server_to_bot", [f"preflop|{a_blind}|<0,0><1,1>"]),
        _event(0.1, "B", "server_to_bot", [f"preflop|{b_blind}|<2,2><3,3>"]),
    ]
    if stage != "preflop":
        events.extend([
            _event(0.11, "B", "bot_to_server", ["call"]),
            _event(0.12, "A", "server_to_bot", ["call"]),
            _event(0.13, "A", "bot_to_server", ["check"]),
            _event(0.14, "B", "server_to_bot", ["check"]),
        ])
    board = {
        "flop": ["flop|<0,4><1,5><2,6>"],
        "turn": [
            *( [] if malformed_prefix else ["flop|<0,4><1,5><2,6>"] ),
            "turn|<3,7>",
        ],
    }.get(stage, [])
    for index, message in enumerate(board, 1):
        events.extend([
            _event(0.1 + index * 0.1, "A", "server_to_bot", [message]),
            _event(0.15 + index * 0.1, "B", "server_to_bot", [message]),
        ])
        if stage == "turn" and message.startswith("flop|"):
            events.extend([
                _event(0.26, "A", "bot_to_server", ["check"]),
                _event(0.27, "B", "server_to_bot", ["check"]),
                _event(0.28, "B", "bot_to_server", ["call"]),
            ])
    events.extend([
        _event(1, "A", "bot_to_server", ["allin"]),
        _event(1.1, "B", "server_to_bot", ["allin"]),
    ])
    if duplicate_allin_response_on_a:
        events.extend([
            _event(1.11, "A", "server_to_bot", ["allin"]),
            _event(1.12, "A", "bot_to_server", ["call"]),
        ])
    if include_call:
        events.append(_event(1.2, "B", "bot_to_server", [response]))
    a_settlement = _event(
        2,
        "A",
        "server_to_bot",
        [f"earnChips {a_settlement_amount}"],
    )
    b_settlement = _event(
        2.1,
        "B",
        "server_to_bot",
        [f"earnChips {b_settlement_amount}"],
    )
    a_reveal = _event(3, "A", "server_to_bot", ["oppo_hands|<2,2><3,3>"])
    b_reveal = _event(3.1, "B", "server_to_bot", ["oppo_hands|<0,0><1,1>"])
    if reorder_peer_settlement:
        if include_a_settlement:
            events.append(a_settlement)
        events.append(a_reveal)
        if include_b_settlement:
            events.append(b_settlement)
        events.append(b_reveal)
    else:
        if include_a_settlement:
            events.append(a_settlement)
        if include_b_settlement:
            events.append(b_settlement)
        events.extend([a_reveal, b_reveal])
    return events


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
        _event(0.2, "A", "bot_to_server", ["call"]),
        _event(0.21, "B", "server_to_bot", ["call"]),
        _event(0.22, "B", "bot_to_server", ["check"]),
    ]
    timestamp = 1.0
    for stage in ("flop", "turn", "river"):
        events.append(
            _event(timestamp, "A", "server_to_bot", [board_a[stage]])
        )
        events.append(
            _event(timestamp + 0.1, "B", "server_to_bot", [board_b[stage]])
        )
        if stage in {"flop", "turn"}:
            events.extend([
                _event(timestamp + 0.2, "B", "bot_to_server", ["check"]),
                _event(timestamp + 0.21, "A", "server_to_bot", ["check"]),
                _event(timestamp + 0.22, "A", "bot_to_server", ["call"]),
            ])
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


@pytest.mark.parametrize(
    "stage, public_cards_observed",
    [("preflop", 0), ("flop", 3), ("turn", 4)],
)
def test_replay_accepts_cross_connection_settled_called_allin_without_runout(
    stage,
    public_cards_observed,
):
    summary = replay_events(
        _called_allin_without_runout_events(stage=stage),
        finalized=True,
    )

    assert summary["issues"] == []
    warnings = [
        item
        for item in summary["warnings"]
        if item["kind"] == "showdown_runout_omitted_after_called_allin"
    ]
    assert {item["conn"] for item in warnings} == {"A", "B"}
    assert {item["public_cards_observed"] for item in warnings} == {
        public_cards_observed
    }
    assert {
        item["public_cards_observed"]
        for item in summary["omitted_allin_runout_boundaries"]
    } == {public_cards_observed}


@pytest.mark.parametrize(
    "a_amount,b_amount",
    [(20000, -20000), (-20000, 20000), (0, 0)],
)
def test_replay_accepts_only_exact_called_allin_net_settlements(
    a_amount,
    b_amount,
):
    summary = replay_events(
        _called_allin_without_runout_events(
            a_settlement_amount=a_amount,
            b_settlement_amount=b_amount,
        ),
        finalized=True,
    )

    assert summary["issues"] == []
    assert len(summary["omitted_allin_runout_boundaries"]) == 2


def test_replay_defers_cross_socket_settlement_order_until_final_binding():
    events = _called_allin_without_runout_events(reorder_peer_settlement=True)
    provisional = replay_events(events, finalized=False)
    summary = replay_events(
        events,
        finalized=True,
    )

    assert provisional["issues"] == []
    assert provisional["omitted_allin_runout_boundaries"] == []
    assert len(
        provisional["provisional_omitted_allin_runout_boundaries"]
    ) == 2
    assert summary["issues"] == []
    assert len(summary["omitted_allin_runout_boundaries"]) == 2
    assert summary["provisional_omitted_allin_runout_boundaries"] == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"include_a_settlement": False},
        {"include_b_settlement": False},
        {"include_call": False},
        {"fold_instead": True},
        {"stage": "turn", "malformed_prefix": True},
        {"b_settlement_amount": -19999},
        {"a_settlement_amount": 19999, "b_settlement_amount": -19999},
        {"a_settlement_amount": 20001, "b_settlement_amount": -20001},
        {"duplicate_allin_response_on_a": True},
    ],
)
def test_replay_rejects_unproved_missing_runout_showdown(overrides):
    summary = replay_events(
        _called_allin_without_runout_events(**overrides),
        finalized=True,
    )

    assert any(
        issue["kind"] == "showdown_boundary_invalid"
        or issue["kind"] == "showdown_cross_connection_boundary_mismatch"
        for issue in summary["issues"]
    )


def test_replay_keeps_unpaired_called_allin_reveal_provisional_until_finalized():
    events = [
        event
        for event in _called_allin_without_runout_events()
        if not (
            event["conn"] == "B"
            and event["messages"][0].startswith("oppo_hands|")
        )
    ]

    provisional = replay_events(events, finalized=False)
    finalized = replay_events(events, finalized=True)

    assert provisional["omitted_allin_runout_boundaries"] == []
    assert not any(
        issue["kind"] == "showdown_cross_connection_boundary_unproved"
        for issue in provisional["issues"]
    )
    assert any(
        issue["kind"] == "showdown_cross_connection_boundary_unproved"
        for issue in finalized["issues"]
    )


def test_replay_rejects_prior_street_allin_call_as_current_street_omission():
    events = _called_allin_without_runout_events()
    settlement_index = next(
        index
        for index, event in enumerate(events)
        if event["messages"][0].startswith("earnChips")
    )
    events[settlement_index:settlement_index] = [
        _event(1.3, "A", "server_to_bot", ["flop|<0,4><1,5><2,6>"]),
        _event(1.4, "B", "server_to_bot", ["flop|<0,4><1,5><2,6>"]),
    ]

    summary = replay_events(events, finalized=True)

    assert summary["omitted_allin_runout_boundaries"] == []
    assert any(
        issue["kind"] == "showdown_boundary_invalid"
        for issue in summary["issues"]
    )


def test_replay_rejects_turn_after_unclosed_flop_before_called_allin():
    events = [
        event
        for event in _called_allin_without_runout_events(stage="turn")
        if not 0.25 < float(event["t"]) < 0.3
    ]

    provisional = replay_events(events, finalized=False)
    summary = replay_events(events, finalized=True)

    assert any(
        issue["kind"] == "street_boundary_unproved"
        for issue in provisional["issues"]
    )
    assert summary["omitted_allin_runout_boundaries"] == []
    assert any(
        issue["kind"] == "street_boundary_unproved"
        and issue["previous_stage"] == "flop"
        and issue["observed_stage"] == "turn"
        for issue in summary["issues"]
    )


def test_replay_accepts_hand70_omission_only_as_cross_bound_provisional_wire_proof():
    events = []
    t = 0.0
    for _hand in range(1, 70):
        a_is_small = _hand % 2 == 1
        a_blind = "SMALLBLIND" if a_is_small else "BIGBLIND"
        b_blind = "BIGBLIND" if a_is_small else "SMALLBLIND"
        folder = "A" if a_is_small else "B"
        peer = "B" if a_is_small else "A"
        a_earn = -50 if a_is_small else 50
        b_earn = 50 if a_is_small else -50
        events.extend([
            _event(t, "A", "server_to_bot", [f"preflop|{a_blind}|<0,0><1,1>"]),
            _event(t + 0.01, "B", "server_to_bot", [f"preflop|{b_blind}|<2,2><3,3>"]),
            _event(t + 0.02, folder, "bot_to_server", ["fold"]),
            _event(t + 0.03, peer, "server_to_bot", ["fold"]),
            _event(t + 0.04, "A", "server_to_bot", [f"earnChips {a_earn}"]),
            _event(t + 0.05, "B", "server_to_bot", [f"earnChips {b_earn}"]),
        ])
        t += 0.1
    events.extend([
        _event(t, "A", "server_to_bot", ["preflop|BIGBLIND|<0,0><1,1>"]),
        _event(t + 0.01, "B", "server_to_bot", ["preflop|SMALLBLIND|<2,2><3,3>"]),
        _event(t + 0.02, "B", "bot_to_server", ["allin"]),
        _event(t + 0.03, "A", "server_to_bot", ["allin"]),
        _event(t + 0.04, "A", "bot_to_server", ["call"]),
        _event(t + 0.05, "A", "server_to_bot", ["oppo_hands|<2,2><3,3>"]),
        _event(t + 0.06, "B", "server_to_bot", ["oppo_hands|<0,0><1,1>"]),
    ])

    summary = replay_events(events, finalized=True)

    assert summary["issues"] == []
    assert summary["hands_started_min"] == 70
    assert summary["settlements_min"] == 69
    assert len(summary["omitted_allin_runout_boundaries"]) == 2
    assert all(
        item["natural_hand_70"] is True
        and item["settlement_amount"] is None
        for item in summary["omitted_allin_runout_boundaries"]
    )


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


def test_wire_probe_forwards_bytes_even_if_recorder_fails(tmp_path):
    class Reader:
        async def read(self, _size):
            return b"call"

    class Writer:
        def __init__(self):
            self.payload = bytearray()
            self.drains = 0

        def write(self, payload):
            self.payload.extend(payload)

        async def drain(self):
            self.drains += 1

    class FailingRecorder(WireEventRecorder):
        def record(self, **kwargs):
            if kwargs.get("raw"):
                raise RuntimeError("synthetic recorder failure")
            return super().record(**kwargs)

    recorder = FailingRecorder(tmp_path / "wire.jsonl")
    probe = TcpWireProbe(
        platform_host="127.0.0.1",
        platform_port=1,
        recorder=recorder,
    )
    probe._awaiting_name["A"] = False
    writer = Writer()
    try:
        with pytest.raises(RuntimeError, match="synthetic recorder failure"):
            asyncio.run(
                probe._pipe(
                    "A",
                    "bot_to_server",
                    Reader(),
                    writer,
                )
            )
    finally:
        recorder.close()

    assert bytes(writer.payload) == b"call"
    assert writer.drains == 1


def test_full_two_port_probe_preserves_causal_relay_and_final_marker(tmp_path):
    async def upstream_handler(reader, writer):
        try:
            writer.write(b"name")
            await writer.drain()
            name = (await asyncio.wait_for(reader.read(128), timeout=1)).decode()
            response = {
                "BotA": "preflop|SMALLBLIND|<0,1><1,2>",
                "BotB": "preflop|BIGBLIND|<2,3><3,4>",
            }[name]
            writer.write(response.encode())
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    async def bot(port, name):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await asyncio.wait_for(reader.readexactly(4), timeout=1) == b"name"
        writer.write(name.encode())
        await writer.drain()
        response = (await asyncio.wait_for(reader.read(128), timeout=1)).decode()
        await asyncio.sleep(0.08)
        writer.close()
        await writer.wait_closed()
        return response

    async def exercise(recorder):
        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]
        probe = TcpWireProbe(
            platform_host="127.0.0.1",
            platform_port=upstream_port,
            recorder=recorder,
        )
        try:
            ports = await probe.start()
            responses = await asyncio.gather(
                bot(ports["A"], "BotA"),
                bot(ports["B"], "BotB"),
            )
            await asyncio.sleep(0.05)
            return responses
        finally:
            await probe.stop()
            upstream.close()
            await upstream.wait_closed()

    recorder = WireEventRecorder(tmp_path / "wire.jsonl")
    try:
        responses = asyncio.run(exercise(recorder))
        summary = replay_events(list(recorder.events), finalized=True)
    finally:
        recorder.close()

    assert responses == [
        "preflop|SMALLBLIND|<0,1><1,2>",
        "preflop|BIGBLIND|<2,3><3,4>",
    ]
    assert recorder.events[-1]["event_type"] == "capture_finalized"
    assert summary["issues"] == []
    assert summary["seats"]["A"]["name"] == "BotA"
    assert summary["seats"]["B"]["name"] == "BotB"
    assert summary["hands_started_min"] == 1
