from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from bots.research_native_lab.common_contracts.deal_generator import (
    build_70_hand_commitment,
)
from bots.research_native_lab.common_contracts.native_harness import (
    run_development_tcp_capture_sync,
)
from bots.research_native_lab.common_contracts.native_wire import (
    WireEvidenceError,
    verify_decision_enforcement_events,
    verify_structured_wire_capture,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _resource_digest(value: object) -> str:
    return hashlib.sha256(_canonical(value) + b"\n").hexdigest()


def _decision_enforcement_events(capture: object) -> list[dict[str, object]]:
    payload = json.loads(capture.raw_replay)
    wire = json.loads(capture.raw_wire)
    records = wire["records"]
    inbound_streams = [bytearray(), bytearray()]
    for record in records:
        if record["direction"] == "bot_to_server":
            inbound_streams[record["connection_index"]].extend(
                bytes.fromhex(record["payload_hex"])
            )
    tokens = {token["sequence"]: token for token in wire["tokens"]}
    capture_session = hashlib.sha256(b"native-wire-test-capture-session").hexdigest()
    pending: dict[str, object] | None = None
    result: list[dict[str, object]] = []
    for replay_event in payload["events"]:
        if replay_event.get("type") == "action_requested":
            assert pending is None
            pending = replay_event
            continue
        if replay_event.get("type") != "action":
            continue
        assert pending is not None
        assert replay_event["action"] != "timeout"
        global_index = len(result)
        connection = replay_event["player_idx"]
        request_sequence = pending["decision_open_record_sequence"]
        action_sequence = replay_event["wire_source_record_sequence"]
        token = tokens[replay_event["wire_token_sequence"]]
        close_record = next(
            record
            for record in records[action_sequence + 1 :]
            if record["direction"] == "server_to_bot"
            and record["connection_index"] == 1 - connection
        )
        action_token = bytes(inbound_streams[connection])[
            token["stream_start"] : token["stream_end"]
        ]
        action_digest = hashlib.sha256(action_token).hexdigest()
        event: dict[str, object] = {
            "schema": "pok-supervisor-decision-enforcement-event-v3",
            "capture_session_digest": capture_session,
            "connection_index": connection,
            "hand_index": pending["hand"] - 1,
            "street": pending["stage"],
            "decision_index": global_index,
            "request_raw_record_seq": request_sequence,
            "action_raw_record_seq": action_sequence,
            "decision_close_raw_record_seq": close_record["sequence"],
            "request_token_digest": hashlib.sha256(
                bytes.fromhex(records[request_sequence]["payload_hex"])
            ).hexdigest(),
            "action_token_digest": action_digest,
            "requested_monotonic_ns": pending["requested_monotonic_ns"],
            "action_sent_monotonic_ns": replay_event["action_monotonic_ns"],
            "decision_close_monotonic_ns": close_record["monotonic_ns"],
            "fault_kind": "none",
        }
        event["decision_id"] = _resource_digest(
            {
                "action_raw_record_seq": action_sequence,
                "action_token_digest": action_digest,
                "capture_session_digest": capture_session,
                "connection_index": connection,
                "decision_close_raw_record_seq": close_record["sequence"],
                "decision_index": global_index,
                "hand_index": event["hand_index"],
                "request_raw_record_seq": request_sequence,
                "schema": "pok-supervisor-decision-identity-v3",
                "street": event["street"],
            }
        )
        result.append(event)
        pending = None
    assert pending is None
    return result


def _valid_timeout_capture() -> tuple[bytes, dict[str, object], dict[str, object]]:
    open_monotonic_ns = 100_000_000
    timeout_monotonic_ns = open_monotonic_ns + 60_000_000_000
    bot_names = ("TimeoutA", "TimeoutB")
    record_values = (
        (0, "server_to_bot", b"name\n", 1),
        (1, "server_to_bot", b"name\n", 2),
        (0, "bot_to_server", bot_names[0].encode("ascii"), 3),
        (1, "bot_to_server", bot_names[1].encode("ascii"), 4),
        (0, "server_to_bot", b"preflop|SMALLBLIND|<0,0><1,1>\n", open_monotonic_ns),
        (1, "server_to_bot", b"preflop|BIGBLIND|<2,2><3,3>\n", open_monotonic_ns + 1),
        (1, "server_to_bot", b"fold\n", timeout_monotonic_ns),
    )
    records = [
        {
            "sequence": sequence,
            "connection_index": connection,
            "direction": direction,
            "epoch_ms": 1_000 + sequence,
            "monotonic_ns": monotonic_ns,
            "payload_hex": raw.hex(),
        }
        for sequence, (connection, direction, raw, monotonic_ns) in enumerate(
            record_values
        )
    ]
    tokens = []
    for sequence, (connection, name, source_sequence) in enumerate(
        ((0, bot_names[0], 2), (1, bot_names[1], 3))
    ):
        tokens.append(
            {
                "sequence": sequence,
                "connection_index": connection,
                "message_type": "name",
                "stream_start": 0,
                "stream_end": len(name),
                "source_record_sequence": source_sequence,
                "completed_epoch_ms": records[source_sequence]["epoch_ms"],
                "completed_monotonic_ns": records[source_sequence]["monotonic_ns"],
            }
        )
    wire = {
        "schema": "pok-native-wire-evidence-v3",
        "framing": "local-native-server-write-v1",
        "records": records,
        "tokens": tokens,
    }
    payload: dict[str, object] = {
        "bot_a": bot_names[0],
        "bot_b": bot_names[1],
        "events": [
            {"type": "hand_start", "hand": 1, "sb_idx": 0, "bb_idx": 1},
            {
                "type": "cards_dealt",
                "hole_cards": [["<0,0>", "<1,1>"], ["<2,2>", "<3,3>"]],
            },
            {
                "type": "action_requested",
                "player_idx": 0,
                "hand": 1,
                "stage": "preflop",
                "decision_open_record_sequence": 4,
                "requested_epoch_ms": records[4]["epoch_ms"],
                "requested_monotonic_ns": open_monotonic_ns,
                "platform_action_timeout_ms": 60_000,
            },
            {
                "type": "action",
                "player_idx": 0,
                "hand": 1,
                "stage": "preflop",
                "decision_index": 0,
                "action": "timeout",
                "action_epoch_ms": records[6]["epoch_ms"],
                "action_monotonic_ns": timeout_monotonic_ns,
            },
        ],
    }
    capture_session = hashlib.sha256(b"native-wire-timeout-session").hexdigest()
    timeout_event: dict[str, object] = {
        "schema": "pok-supervisor-decision-enforcement-event-v3",
        "capture_session_digest": capture_session,
        "connection_index": 0,
        "hand_index": 0,
        "street": "preflop",
        "decision_index": 0,
        "request_raw_record_seq": 4,
        "action_raw_record_seq": None,
        "decision_close_raw_record_seq": 6,
        "request_token_digest": hashlib.sha256(
            bytes.fromhex(records[4]["payload_hex"])
        ).hexdigest(),
        "action_token_digest": None,
        "requested_monotonic_ns": open_monotonic_ns,
        "action_sent_monotonic_ns": None,
        "decision_close_monotonic_ns": timeout_monotonic_ns,
        "fault_kind": "timeout",
    }
    timeout_event["decision_id"] = _resource_digest(
        {
            "action_raw_record_seq": None,
            "action_token_digest": None,
            "capture_session_digest": capture_session,
            "connection_index": 0,
            "decision_close_raw_record_seq": 6,
            "decision_index": 0,
            "hand_index": 0,
            "request_raw_record_seq": 4,
            "schema": "pok-supervisor-decision-identity-v3",
            "street": "preflop",
        }
    )
    return _canonical(wire), payload, timeout_event


def _valid_tokenless_fault_capture(
    action: str,
    fault_kind: str,
) -> tuple[bytes, dict[str, object], dict[str, object]]:
    raw_wire, payload, event = _valid_timeout_capture()
    wire = json.loads(raw_wire)
    open_record = wire["records"][4]
    close_record = wire["records"][6]
    close_record["monotonic_ns"] = open_record["monotonic_ns"] + 1_000_000
    close_record["epoch_ms"] = open_record["epoch_ms"] + 1
    action_event = payload["events"][-1]  # type: ignore[index]
    assert isinstance(action_event, dict)
    action_event["action"] = action
    action_event["action_monotonic_ns"] = close_record["monotonic_ns"]
    action_event["action_epoch_ms"] = close_record["epoch_ms"]
    event["fault_kind"] = fault_kind
    event["decision_close_monotonic_ns"] = close_record["monotonic_ns"]
    return _canonical(wire), payload, event


@pytest.fixture(scope="module")
def capture() -> object:
    commitment = build_70_hand_commitment(
        int.from_bytes(hashlib.sha256(b"native-wire-proof-root").digest(), "big")
    )
    return run_development_tcp_capture_sync(commitment, decision_budget_ms=250)


def test_structured_wire_derives_replay_messages_and_action_timestamps(capture: object) -> None:
    payload = json.loads(capture.raw_replay)
    verified = verify_structured_wire_capture(capture.raw_wire, payload)
    assert verified.record_count > 400
    assert verified.token_count == 72
    assert verified.bot_token_count_by_connection == (36, 36)
    assert verified.server_message_count_by_connection == (176, 176)


def test_structured_wire_rejects_message_token_and_timing_grafts(capture: object) -> None:
    payload = json.loads(capture.raw_replay)

    wrong_server = json.loads(capture.raw_wire)
    preflop = next(
        row
        for row in wrong_server["records"]
        if bytes.fromhex(row["payload_hex"]).startswith(b"preflop|")
    )
    preflop["payload_hex"] = bytes.fromhex(preflop["payload_hex"]).replace(
        b"SMALLBLIND", b"BIGBLIND"
    ).hex()
    with pytest.raises(WireEvidenceError, match="server-write order/bytes differ"):
        verify_structured_wire_capture(_canonical(wrong_server), payload)

    wrong_client = json.loads(capture.raw_wire)
    action_record = next(
        row
        for row in wrong_client["records"]
        if row["direction"] == "bot_to_server"
        and bytes.fromhex(row["payload_hex"]) == b"fold"
    )
    action_record["payload_hex"] = b"call".hex()
    with pytest.raises(WireEvidenceError, match="bytes differ from replay action"):
        verify_structured_wire_capture(_canonical(wrong_client), payload)

    wrong_timing = json.loads(capture.raw_wire)
    first_action = next(
        row for row in wrong_timing["tokens"] if row["message_type"] == "action"
    )
    first_action["completed_monotonic_ns"] += 1
    with pytest.raises(WireEvidenceError, match="ingress arrival"):
        verify_structured_wire_capture(_canonical(wrong_timing), payload)


def test_structured_wire_rejects_uncommitted_or_mislinked_client_bytes(capture: object) -> None:
    payload = json.loads(capture.raw_replay)
    extra = json.loads(capture.raw_wire)
    last_client = next(
        row for row in reversed(extra["records"]) if row["direction"] == "bot_to_server"
    )
    last_client["payload_hex"] += b"call".hex()
    with pytest.raises(WireEvidenceError, match="uncommitted bytes"):
        verify_structured_wire_capture(_canonical(extra), payload)

    mislinked = json.loads(capture.raw_wire)
    action = next(
        row for row in mislinked["tokens"] if row["message_type"] == "action"
    )
    action["source_record_sequence"] = 0
    with pytest.raises(WireEvidenceError, match="completing raw record"):
        verify_structured_wire_capture(_canonical(mislinked), payload)


def test_global_reorder_cannot_move_action_after_its_peer_relay(capture: object) -> None:
    payload = json.loads(capture.raw_replay)
    wire = json.loads(capture.raw_wire)
    token = next(row for row in wire["tokens"] if row["message_type"] == "action")
    source_sequence = token["source_record_sequence"]
    close_sequence = next(
        row["sequence"]
        for row in wire["records"][source_sequence + 1 :]
        if row["direction"] == "server_to_bot"
    )
    source = wire["records"][source_sequence]
    close = wire["records"][close_sequence]
    source_content = {
        key: source[key]
        for key in ("connection_index", "direction", "payload_hex")
    }
    close_content = {
        key: close[key]
        for key in ("connection_index", "direction", "payload_hex")
    }
    source.update(close_content)
    close.update(source_content)
    token["source_record_sequence"] = close_sequence
    token["completed_epoch_ms"] = close["epoch_ms"]
    token["completed_monotonic_ns"] = close["monotonic_ns"]
    action = next(
        event
        for event in payload["events"]
        if event.get("type") == "action"
        and event.get("wire_token_sequence") == token["sequence"]
    )
    action["wire_source_record_sequence"] = close_sequence
    action["action_epoch_ms"] = close["epoch_ms"]
    action["action_monotonic_ns"] = close["monotonic_ns"]
    with pytest.raises(WireEvidenceError, match="decision-open/close lease"):
        verify_structured_wire_capture(_canonical(wire), payload)


def test_request_clock_cannot_be_shifted_away_from_server_boundary(capture: object) -> None:
    payload = json.loads(capture.raw_replay)
    request = next(
        event for event in payload["events"] if event.get("type") == "action_requested"
    )
    action = next(
        event
        for event in payload["events"]
        if event.get("type") == "action"
        and event.get("hand") == request["hand"]
        and event.get("player_idx") == request["player_idx"]
    )
    shifted = action["action_monotonic_ns"] - 1
    request["requested_monotonic_ns"] = shifted
    request["compute_deadline_monotonic_ns"] = shifted + 250_000_000
    request["platform_deadline_monotonic_ns"] = shifted + 60_000_000_000
    request["requested_epoch_ms"] = action["action_epoch_ms"]
    request["compute_deadline_epoch_ms"] = action["action_epoch_ms"] + 250
    request["deadline_epoch_ms"] = action["action_epoch_ms"] + 60_000
    with pytest.raises(WireEvidenceError, match="server-write boundary"):
        verify_structured_wire_capture(capture.raw_wire, payload)


def test_decision_enforcement_trace_binds_every_wire_decision(capture: object) -> None:
    payload = json.loads(capture.raw_replay)
    events = [SimpleNamespace(**row) for row in _decision_enforcement_events(capture)]
    verified = verify_decision_enforcement_events(capture.raw_wire, payload, events)
    assert verified.decision_count == 70
    assert verified.capture_session_digest == events[0].capture_session_digest
    assert len(bytes.fromhex(verified.binding_digest)) == 32


def test_decision_enforcement_trace_requires_resource_event_v3(capture: object) -> None:
    payload = json.loads(capture.raw_replay)
    events = _decision_enforcement_events(capture)
    events[0]["schema"] = "pok-supervisor-decision-enforcement-event-v1"
    with pytest.raises(WireEvidenceError, match="not the v3 schema"):
        verify_decision_enforcement_events(capture.raw_wire, payload, events)


def test_decision_enforcement_trace_rejects_missing_extra_and_reordered_events(
    capture: object,
) -> None:
    payload = json.loads(capture.raw_replay)
    events = _decision_enforcement_events(capture)
    with pytest.raises(WireEvidenceError, match="cover every replay decision"):
        verify_decision_enforcement_events(capture.raw_wire, payload, events[:-1])
    with pytest.raises(WireEvidenceError, match="cover every replay decision"):
        verify_decision_enforcement_events(
            capture.raw_wire, payload, [*events, dict(events[-1])]
        )
    reordered = [dict(row) for row in events]
    reordered[0], reordered[1] = reordered[1], reordered[0]
    with pytest.raises(WireEvidenceError, match="identity/order"):
        verify_decision_enforcement_events(capture.raw_wire, payload, reordered)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("request_raw_record_seq", lambda value: value + 1, "decision-open"),
        ("action_raw_record_seq", lambda value: value + 1, "token-completing"),
        ("request_token_digest", lambda _value: "0" * 64, "request token digest"),
        ("action_token_digest", lambda _value: "0" * 64, "action token digest"),
        ("requested_monotonic_ns", lambda value: value + 1, "request timestamp"),
        ("action_sent_monotonic_ns", lambda value: value + 1, "action timestamp"),
        (
            "decision_close_raw_record_seq",
            lambda value: value + 1,
            "close raw record",
        ),
        (
            "decision_close_monotonic_ns",
            lambda value: value + 1,
            "close timestamp",
        ),
        ("decision_id", lambda _value: "0" * 64, "decision ID"),
    ),
)
def test_decision_enforcement_trace_rejects_wire_field_tampering(
    capture: object,
    field: str,
    replacement: object,
    message: str,
) -> None:
    payload = json.loads(capture.raw_replay)
    events = _decision_enforcement_events(capture)
    events[0][field] = replacement(events[0][field])
    with pytest.raises(WireEvidenceError, match=message):
        verify_decision_enforcement_events(capture.raw_wire, payload, events)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("decision_index", lambda value: value + 1),
        ("connection_index", lambda value: 1 - value),
        ("hand_index", lambda value: value + 1),
        ("street", lambda _value: "flop"),
    ),
)
def test_decision_enforcement_trace_rejects_identity_tampering(
    capture: object,
    field: str,
    replacement: object,
) -> None:
    payload = json.loads(capture.raw_replay)
    events = _decision_enforcement_events(capture)
    events[0][field] = replacement(events[0][field])
    with pytest.raises(WireEvidenceError, match="identity/order"):
        verify_decision_enforcement_events(capture.raw_wire, payload, events)


def test_decision_enforcement_trace_rejects_replay_local_index_tampering(
    capture: object,
) -> None:
    payload = json.loads(capture.raw_replay)
    action = next(event for event in payload["events"] if event.get("type") == "action")
    action["decision_index"] = 9
    with pytest.raises(WireEvidenceError, match="actor-local index"):
        verify_decision_enforcement_events(
            capture.raw_wire, payload, _decision_enforcement_events(capture)
        )


@pytest.mark.parametrize(
    "field",
    ("action_raw_record_seq", "action_token_digest", "action_sent_monotonic_ns"),
)
def test_decision_enforcement_normal_action_requires_complete_token_triple(
    capture: object,
    field: str,
) -> None:
    payload = json.loads(capture.raw_replay)
    events = _decision_enforcement_events(capture)
    events[0][field] = None
    with pytest.raises(WireEvidenceError, match="non-timeout lacks"):
        verify_decision_enforcement_events(capture.raw_wire, payload, events)


def test_decision_enforcement_timeout_binds_null_action_to_peer_fold_close() -> None:
    raw_wire, payload, event = _valid_timeout_capture()
    verify_structured_wire_capture(raw_wire, payload)
    verified = verify_decision_enforcement_events(raw_wire, payload, (event,))
    assert verified.decision_count == 1
    assert verified.capture_session_digest == event["capture_session_digest"]


def test_decision_enforcement_timeout_rejects_fabricated_client_token() -> None:
    raw_wire, payload, event = _valid_timeout_capture()
    fabricated = dict(event)
    fabricated["action_raw_record_seq"] = 5
    fabricated["action_token_digest"] = hashlib.sha256(b"fold").hexdigest()
    fabricated["action_sent_monotonic_ns"] = event[
        "decision_close_monotonic_ns"
    ]
    with pytest.raises(WireEvidenceError, match="tokenless fault fabricates"):
        verify_decision_enforcement_events(raw_wire, payload, (fabricated,))


def test_decision_enforcement_timeout_rejects_wrong_close_boundary() -> None:
    raw_wire, payload, event = _valid_timeout_capture()
    wrong_close = dict(event)
    wrong_close["decision_close_raw_record_seq"] = 5
    wrong_close["decision_close_monotonic_ns"] = 100_000_001
    with pytest.raises(WireEvidenceError, match="close raw record"):
        verify_decision_enforcement_events(raw_wire, payload, (wrong_close,))


@pytest.mark.parametrize(
    ("action", "fault_kind"),
    (
        ("fault:crash", "crash"),
        ("fault:resource_overrun", "resource"),
        ("fault:protocol", "protocol"),
        ("fault:infrastructure", "infrastructure"),
    ),
)
def test_decision_enforcement_tokenless_fault_binds_peer_fold_close(
    action: str,
    fault_kind: str,
) -> None:
    raw_wire, payload, event = _valid_tokenless_fault_capture(action, fault_kind)
    verify_structured_wire_capture(raw_wire, payload)
    verified = verify_decision_enforcement_events(raw_wire, payload, (event,))
    assert verified.decision_count == 1

    wrong_kind = dict(event)
    wrong_kind["fault_kind"] = "timeout"
    with pytest.raises(WireEvidenceError, match="does not classify"):
        verify_decision_enforcement_events(raw_wire, payload, (wrong_kind,))

    at_deadline_wire = json.loads(raw_wire)
    open_ns = at_deadline_wire["records"][4]["monotonic_ns"]
    at_deadline_wire["records"][6]["monotonic_ns"] = open_ns + 60_000_000_000
    at_deadline_payload = json.loads(json.dumps(payload))
    at_deadline_payload["events"][-1]["action_monotonic_ns"] = (
        open_ns + 60_000_000_000
    )
    with pytest.raises(WireEvidenceError, match="timeout attribution takes precedence"):
        verify_structured_wire_capture(
            _canonical(at_deadline_wire),
            at_deadline_payload,
        )
