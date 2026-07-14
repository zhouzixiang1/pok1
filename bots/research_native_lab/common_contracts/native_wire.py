"""Strict raw-wire evidence parser and replay correlation.

The replay JSON is an observer projection, not a source of truth by itself.
This module independently parses the byte-capture envelope and proves that
the projection's names, private/public cards, actions and settlements are the
messages actually read from or written to the two native TCP connections.

The schema intentionally describes the local native-strength transport.  It
does not claim to be an official-EXE packet capture: local server writes are
LF-delimited and each successful write/drain call is one evidence record,
while client reads remain arbitrary TCP chunks.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Sequence


WIRE_CAPTURE_SCHEMA = "pok-native-wire-evidence-v3"
WIRE_CAPTURE_FRAMING = "local-native-server-write-v1"
DECISION_REQUEST_TOKEN_DIGEST_SPEC = (
    "sha256(exact-server-to-bot-record-payload-including-terminal-lf)"
)
DECISION_ACTION_TOKEN_DIGEST_SPEC = (
    "sha256(exact-parser-committed-client-action-token-span-excluding-separators)"
)
DECISION_ENFORCEMENT_EVENT_SCHEMA = (
    "pok-supervisor-decision-enforcement-event-v3"
)
DECISION_IDENTITY_SCHEMA = "pok-supervisor-decision-identity-v3"
_NO_CLIENT_TOKEN_FAULT_ACTIONS = {
    "timeout": "timeout",
    "fault:crash": "crash",
    "fault:resource_overrun": "resource",
    "fault:protocol": "protocol",
    "fault:infrastructure": "infrastructure",
}
MAX_WIRE_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_WIRE_RECORDS = 200_000
MAX_WIRE_TOKENS = 100_000
MAX_WIRE_PAYLOAD_BYTES = 65_536


class WireEvidenceError(ValueError):
    """Raw bytes cannot prove the claimed native replay projection."""


@dataclass(frozen=True, slots=True)
class ParsedWireRecord:
    sequence: int
    connection_index: int
    direction: str
    epoch_ms: int
    monotonic_ns: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class ParsedWireToken:
    sequence: int
    connection_index: int
    message_type: str
    stream_start: int
    stream_end: int
    source_record_sequence: int
    completed_epoch_ms: int
    completed_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class VerifiedWireSemantics:
    semantic_binding_digest: str
    record_count: int
    token_count: int
    server_message_count_by_connection: tuple[int, int]
    bot_token_count_by_connection: tuple[int, int]


@dataclass(frozen=True, slots=True)
class VerifiedDecisionEnforcementBinding:
    """Wire-derived identity of one complete signed decision-event trace.

    The capture session itself is signed by the external supervisor and is
    not present in the v3 wire envelope.  This verifier nevertheless requires
    one canonical session digest across the trace and includes it in this
    binding; the caller must separately bind that digest to the authorized
    supervisor leg.
    """

    binding_digest: str
    capture_session_digest: str
    decision_count: int


@dataclass(frozen=True, slots=True)
class _ExpectedBotToken:
    message_type: str
    payload: str
    action_event: Mapping[str, Any] | None
    decision_index: int | None = None


@dataclass(frozen=True, slots=True)
class _ExpectedDecision:
    connection_index: int
    request_event: Mapping[str, Any]
    action_event: Mapping[str, Any]
    open_server_ordinal: int
    peer_relay_server_ordinal: int
    bot_token_index: int | None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _reject_constant(value: str) -> None:
    raise WireEvidenceError(f"raw wire JSON contains non-finite number {value}")


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WireEvidenceError(f"raw wire JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _strict_load(raw_wire: bytes) -> Mapping[str, Any]:
    if type(raw_wire) is not bytes or not raw_wire:
        raise WireEvidenceError("raw wire capture must be non-empty bytes")
    if len(raw_wire) > MAX_WIRE_CAPTURE_BYTES:
        raise WireEvidenceError("raw wire capture exceeds the frozen size limit")
    try:
        text = raw_wire.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WireEvidenceError("raw wire envelope is not strict UTF-8 JSON") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except WireEvidenceError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise WireEvidenceError("raw wire envelope is not strict JSON") from exc
    if not isinstance(value, Mapping):
        raise WireEvidenceError("raw wire envelope must be a JSON object")
    return value


def is_structured_wire_capture(raw_wire: bytes) -> bool:
    """Return whether bytes declare this exact evidence schema.

    Parsing failures are deliberately reported as ``False`` here; callers
    that require formal evidence must then fail closed.  Once the schema name
    is visible, full verification never falls back to opaque bytes.
    """

    if type(raw_wire) is not bytes or not raw_wire.startswith(b"{"):
        return False
    try:
        value = _strict_load(raw_wire)
    except WireEvidenceError:
        return False
    return value.get("schema") == WIRE_CAPTURE_SCHEMA


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise WireEvidenceError(
            f"{name} fields differ from schema; missing={missing}, extra={extra}"
        )


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise WireEvidenceError(
            f"{name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _parse_records(rows: Any) -> tuple[ParsedWireRecord, ...]:
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_WIRE_RECORDS:
        raise WireEvidenceError("raw wire records must be a non-empty bounded list")
    parsed: list[ParsedWireRecord] = []
    previous_monotonic = 0
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise WireEvidenceError(f"raw wire record {index} must be an object")
        _exact_keys(
            row,
            {
                "sequence",
                "connection_index",
                "direction",
                "epoch_ms",
                "monotonic_ns",
                "payload_hex",
            },
            f"raw wire record {index}",
        )
        sequence = _integer(
            row["sequence"], f"raw wire record {index} sequence", minimum=0,
            maximum=MAX_WIRE_RECORDS - 1,
        )
        if sequence != index:
            raise WireEvidenceError("raw wire record sequence is not contiguous")
        connection = _integer(
            row["connection_index"],
            f"raw wire record {index} connection",
            minimum=0,
            maximum=1,
        )
        direction = row["direction"]
        if direction not in {"server_to_bot", "bot_to_server"}:
            raise WireEvidenceError(f"raw wire record {index} has unknown direction")
        epoch_ms = _integer(
            row["epoch_ms"],
            f"raw wire record {index} epoch_ms",
            minimum=1,
            maximum=(1 << 63) - 1,
        )
        monotonic_ns = _integer(
            row["monotonic_ns"],
            f"raw wire record {index} monotonic_ns",
            minimum=1,
            maximum=(1 << 63) - 1,
        )
        if monotonic_ns < previous_monotonic:
            raise WireEvidenceError("raw wire record timestamps move backwards")
        previous_monotonic = monotonic_ns
        payload_hex = row["payload_hex"]
        if not isinstance(payload_hex, str) or not payload_hex:
            raise WireEvidenceError(f"raw wire record {index} payload is empty")
        if payload_hex != payload_hex.lower() or len(payload_hex) % 2:
            raise WireEvidenceError(f"raw wire record {index} payload hex is not canonical")
        try:
            payload = bytes.fromhex(payload_hex)
        except ValueError as exc:
            raise WireEvidenceError(
                f"raw wire record {index} payload is not hexadecimal"
            ) from exc
        if not payload or len(payload) > MAX_WIRE_PAYLOAD_BYTES:
            raise WireEvidenceError(
                f"raw wire record {index} payload length is outside the frozen limit"
            )
        if direction == "server_to_bot":
            try:
                decoded = payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise WireEvidenceError("server write is not strict UTF-8") from exc
            if not decoded.endswith("\n") or decoded.count("\n") != 1:
                raise WireEvidenceError(
                    "each local server write must contain exactly one LF-terminated message"
                )
            if "\r" in decoded:
                raise WireEvidenceError("local server writes must not contain CR framing")
        parsed.append(
            ParsedWireRecord(
                sequence=sequence,
                connection_index=connection,
                direction=direction,
                epoch_ms=epoch_ms,
                monotonic_ns=monotonic_ns,
                payload=payload,
            )
        )
    return tuple(parsed)


def _parse_tokens(rows: Any) -> tuple[ParsedWireToken, ...]:
    if not isinstance(rows, list) or not 2 <= len(rows) <= MAX_WIRE_TOKENS:
        raise WireEvidenceError("raw wire tokens must contain both names and be bounded")
    parsed: list[ParsedWireToken] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise WireEvidenceError(f"raw wire token {index} must be an object")
        _exact_keys(
            row,
            {
                "sequence",
                "connection_index",
                "message_type",
                "stream_start",
                "stream_end",
                "source_record_sequence",
                "completed_epoch_ms",
                "completed_monotonic_ns",
            },
            f"raw wire token {index}",
        )
        sequence = _integer(
            row["sequence"], f"raw wire token {index} sequence", minimum=0,
            maximum=MAX_WIRE_TOKENS - 1,
        )
        if sequence != index:
            raise WireEvidenceError("raw wire token sequence is not contiguous")
        connection = _integer(
            row["connection_index"],
            f"raw wire token {index} connection",
            minimum=0,
            maximum=1,
        )
        message_type = row["message_type"]
        if message_type not in {"name", "action"}:
            raise WireEvidenceError(f"raw wire token {index} has unknown message type")
        start = _integer(
            row["stream_start"],
            f"raw wire token {index} start",
            minimum=0,
            maximum=MAX_WIRE_CAPTURE_BYTES,
        )
        end = _integer(
            row["stream_end"],
            f"raw wire token {index} end",
            minimum=1,
            maximum=MAX_WIRE_CAPTURE_BYTES,
        )
        if end <= start:
            raise WireEvidenceError("raw wire token span is empty or reversed")
        parsed.append(
            ParsedWireToken(
                sequence=sequence,
                connection_index=connection,
                message_type=message_type,
                stream_start=start,
                stream_end=end,
                source_record_sequence=_integer(
                    row["source_record_sequence"],
                    f"raw wire token {index} source record",
                    minimum=0,
                    maximum=MAX_WIRE_RECORDS - 1,
                ),
                completed_epoch_ms=_integer(
                    row["completed_epoch_ms"],
                    f"raw wire token {index} completion epoch",
                    minimum=1,
                    maximum=(1 << 63) - 1,
                ),
                completed_monotonic_ns=_integer(
                    row["completed_monotonic_ns"],
                    f"raw wire token {index} completion monotonic",
                    minimum=1,
                    maximum=(1 << 63) - 1,
                ),
            )
        )
    return tuple(parsed)


def _card_rows(value: Any, name: str) -> tuple[str, str]:
    if not isinstance(value, list) or len(value) != 2:
        raise WireEvidenceError(f"{name} must contain exactly two card strings")
    if any(not isinstance(card, str) or not card for card in value):
        raise WireEvidenceError(f"{name} contains a non-string card")
    return value[0], value[1]


def _expected_projection(
    payload: Mapping[str, Any],
) -> tuple[
    tuple[tuple[int, str], ...],
    tuple[tuple[_ExpectedBotToken, ...], tuple[_ExpectedBotToken, ...]],
    tuple[_ExpectedDecision, ...],
]:
    bot_a = payload.get("bot_a")
    bot_b = payload.get("bot_b")
    if not isinstance(bot_a, str) or not bot_a or not isinstance(bot_b, str) or not bot_b:
        raise WireEvidenceError("replay bot labels are unavailable to wire verifier")
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise WireEvidenceError("replay events are unavailable to wire verifier")
    server: list[tuple[int, str]] = []
    last_server_ordinal_by_connection = [-1, -1]

    def append_server(connection: int, message: str) -> int:
        ordinal = len(server)
        server.append((connection, message))
        last_server_ordinal_by_connection[connection] = ordinal
        return ordinal

    append_server(0, "name")
    append_server(1, "name")
    bot: list[list[_ExpectedBotToken]] = [
        [_ExpectedBotToken("name", bot_a, None)],
        [_ExpectedBotToken("name", bot_b, None)],
    ]
    decisions: list[_ExpectedDecision] = []
    pending_request: tuple[int, Mapping[str, Any], int] | None = None
    current_hand: int | None = None
    sb_idx: int | None = None
    bb_idx: int | None = None
    for event_index, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise WireEvidenceError(f"replay event {event_index} is not an object")
        event_type = event.get("type")
        if event_type == "client_order":
            continue
        if event_type == "hand_start":
            current_hand = event.get("hand")
            sb_idx = event.get("sb_idx")
            bb_idx = event.get("bb_idx")
            if type(current_hand) is not int or type(sb_idx) is not int or type(bb_idx) is not int:
                raise WireEvidenceError("hand_start lacks integer hand/seat fields")
            if {sb_idx, bb_idx} != {0, 1}:
                raise WireEvidenceError("hand_start seat mapping is invalid")
            continue
        if event_type == "cards_dealt":
            if current_hand is None or sb_idx is None or bb_idx is None:
                raise WireEvidenceError("cards_dealt precedes hand_start")
            rows = event.get("hole_cards")
            if not isinstance(rows, list) or len(rows) != 2:
                raise WireEvidenceError("cards_dealt lacks two hole-card rows")
            holes = (_card_rows(rows[0], "connection 0 holes"), _card_rows(rows[1], "connection 1 holes"))
            # The local engine writes SB first and BB second.
            append_server(
                sb_idx,
                f"preflop|SMALLBLIND|{''.join(holes[sb_idx])}",
            )
            append_server(
                bb_idx,
                f"preflop|BIGBLIND|{''.join(holes[bb_idx])}",
            )
            continue
        if event_type == "stage":
            stage = event.get("stage")
            cards = event.get("cards")
            if stage not in {"flop", "turn", "river"} or not isinstance(cards, list):
                raise WireEvidenceError("stage event is unavailable to wire verifier")
            if any(not isinstance(card, str) or not card for card in cards):
                raise WireEvidenceError("stage event contains an invalid card")
            message = f"{stage}|{''.join(cards)}"
            append_server(0, message)
            append_server(1, message)
            if current_hand is None:
                raise WireEvidenceError("stage event lacks an active hand")
            continue
        if event_type == "action_requested":
            if pending_request is not None:
                raise WireEvidenceError("wire projection sees overlapping decisions")
            connection = event.get("player_idx")
            hand = event.get("hand")
            stage = event.get("stage")
            if (
                type(connection) is not int
                or connection not in (0, 1)
                or type(hand) is not int
                or stage not in {"preflop", "flop", "turn", "river"}
            ):
                raise WireEvidenceError("action request has invalid hand/stage/actor")
            open_ordinal = last_server_ordinal_by_connection[connection]
            if open_ordinal < 0:
                raise WireEvidenceError("action request lacks an actor-enabling server write")
            pending_request = (connection, event, open_ordinal)
            continue
        if event_type == "action":
            connection = event.get("player_idx")
            if type(connection) is not int or connection not in (0, 1):
                raise WireEvidenceError("action event has invalid connection")
            action = event.get("action")
            if not isinstance(action, str) or not action:
                raise WireEvidenceError("action event lacks raw action text")
            if pending_request is None or pending_request[0] != connection:
                raise WireEvidenceError("action lacks its ordered wire decision request")
            request_connection, request_event, open_ordinal = pending_request
            bot_token_index: int | None = None
            if action in _NO_CLIENT_TOKEN_FAULT_ACTIONS:
                peer_message = "fold"
            elif action.startswith("illegal:"):
                raw = action[len("illegal:") :]
                if not raw:
                    raise WireEvidenceError("illegal action lacks captured client bytes")
                bot_token_index = len(bot[connection])
                bot[connection].append(
                    _ExpectedBotToken("action", raw, event, len(decisions))
                )
                peer_message = "fold"
            else:
                raw = f"raise {event.get('amount')}" if action == "raise" else action
                bot_token_index = len(bot[connection])
                bot[connection].append(
                    _ExpectedBotToken("action", raw, event, len(decisions))
                )
                peer_message = raw
            peer_relay_ordinal = append_server(1 - connection, peer_message)
            decisions.append(
                _ExpectedDecision(
                    connection_index=request_connection,
                    request_event=request_event,
                    action_event=event,
                    open_server_ordinal=open_ordinal,
                    peer_relay_server_ordinal=peer_relay_ordinal,
                    bot_token_index=bot_token_index,
                )
            )
            hand = event.get("hand")
            stage = event.get("stage")
            if type(hand) is not int or not isinstance(stage, str):
                raise WireEvidenceError("action lacks hand/stage for wire causality")
            pending_request = None
            continue
        if event_type == "settle":
            earnings = event.get("earnings")
            if (
                not isinstance(earnings, list)
                or len(earnings) != 2
                or any(type(value) is not int for value in earnings)
            ):
                raise WireEvidenceError("settle event lacks integer earnings")
            for connection in range(2):
                append_server(connection, f"earnChips {earnings[connection]}")
            if event.get("is_showdown") is True:
                sb_cards = _card_rows(event.get("sb_cards"), "showdown SB cards")
                bb_cards = _card_rows(event.get("bb_cards"), "showdown BB cards")
                if sb_idx is None or bb_idx is None:
                    raise WireEvidenceError("showdown lacks active seat mapping")
                hands: list[tuple[str, str] | None] = [None, None]
                hands[sb_idx] = sb_cards
                hands[bb_idx] = bb_cards
                if any(hand is None for hand in hands):
                    raise WireEvidenceError("showdown opponent cards are unavailable")
                append_server(sb_idx, f"oppo_hands|{''.join(hands[bb_idx] or ())}")
                append_server(bb_idx, f"oppo_hands|{''.join(hands[sb_idx] or ())}")
            continue
    if pending_request is not None:
        raise WireEvidenceError("wire projection ends with an open decision")
    return tuple(server), (tuple(bot[0]), tuple(bot[1])), tuple(decisions)


def verify_structured_wire_capture(
    raw_wire: bytes,
    replay_payload: Mapping[str, Any],
) -> VerifiedWireSemantics:
    """Verify the v3 byte envelope and its global decision-causality DAG."""

    envelope = _strict_load(raw_wire)
    _exact_keys(envelope, {"schema", "framing", "records", "tokens"}, "raw wire envelope")
    if envelope["schema"] != WIRE_CAPTURE_SCHEMA:
        raise WireEvidenceError("raw wire envelope uses an unsupported schema")
    if envelope["framing"] != WIRE_CAPTURE_FRAMING:
        raise WireEvidenceError("raw wire envelope uses an unsupported framing contract")
    records = _parse_records(envelope["records"])
    tokens = _parse_tokens(envelope["tokens"])
    expected_server, expected_bot, expected_decisions = _expected_projection(
        replay_payload
    )

    server_records: list[list[ParsedWireRecord]] = [[], []]
    server_records_global: list[ParsedWireRecord] = []
    inbound_streams = [bytearray(), bytearray()]
    inbound_ranges: list[list[tuple[int, int, ParsedWireRecord]]] = [[], []]
    for record in records:
        if record.direction == "server_to_bot":
            server_records[record.connection_index].append(record)
            server_records_global.append(record)
        else:
            connection = record.connection_index
            start = len(inbound_streams[connection])
            inbound_streams[connection].extend(record.payload)
            inbound_ranges[connection].append(
                (start, len(inbound_streams[connection]), record)
            )

    actual_server = tuple(
        (
            record.connection_index,
            record.payload[:-1].decode("utf-8", errors="strict"),
        )
        for record in server_records_global
    )
    if actual_server != expected_server:
        raise WireEvidenceError(
            "global server-write order/bytes differ from replay-derived messages"
        )

    tokens_by_connection: list[list[ParsedWireToken]] = [[], []]
    for token in tokens:
        tokens_by_connection[token.connection_index].append(token)
    for connection in range(2):
        actual = tokens_by_connection[connection]
        expected = expected_bot[connection]
        if len(actual) != len(expected):
            raise WireEvidenceError(
                f"connection {connection} committed token count differs from replay"
            )
        stream = bytes(inbound_streams[connection])
        cursor = 0
        for token_index, (token, expectation) in enumerate(zip(actual, expected, strict=True)):
            if token.message_type != expectation.message_type:
                raise WireEvidenceError(
                    f"connection {connection} token {token_index} type differs from replay"
                )
            separator = stream[cursor : token.stream_start]
            if any(byte not in (10, 13) for byte in separator):
                raise WireEvidenceError("uncommitted client bytes precede a token")
            if token.stream_start < cursor or token.stream_end > len(stream):
                raise WireEvidenceError("client token span is outside its captured stream")
            raw_token = stream[token.stream_start : token.stream_end]
            try:
                decoded = raw_token.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise WireEvidenceError("committed client token is not strict UTF-8") from exc
            if decoded != expectation.payload:
                raise WireEvidenceError(
                    f"connection {connection} token {token_index} bytes differ from replay action"
                )
            source = next(
                (
                    record
                    for start, end, record in inbound_ranges[connection]
                    if start < token.stream_end <= end
                ),
                None,
            )
            if source is None or source.sequence != token.source_record_sequence:
                raise WireEvidenceError("client token is not linked to its completing raw record")
            start_source = next(
                (
                    record
                    for start, end, record in inbound_ranges[connection]
                    if start <= token.stream_start < end
                ),
                None,
            )
            if start_source is None:
                raise WireEvidenceError("client token start is not linked to raw bytes")
            if (
                token.completed_monotonic_ns != source.monotonic_ns
                or token.completed_epoch_ms != source.epoch_ms
            ):
                raise WireEvidenceError(
                    "client token completion is not derived from ingress arrival"
                )
            if expectation.action_event is not None:
                event = expectation.action_event
                if (
                    event.get("action_monotonic_ns") != token.completed_monotonic_ns
                    or event.get("action_epoch_ms") != token.completed_epoch_ms
                ):
                    raise WireEvidenceError(
                        "action event timestamp differs from raw token completion"
                    )
                if expectation.decision_index is None:
                    raise WireEvidenceError("action token lacks a decision edge")
                decision = expected_decisions[expectation.decision_index]
                open_record = server_records_global[
                    decision.open_server_ordinal
                ]
                close_record = server_records_global[
                    decision.peer_relay_server_ordinal
                ]
                if not (
                    open_record.sequence < start_source.sequence
                    <= source.sequence < close_record.sequence
                ):
                    raise WireEvidenceError(
                        "client action bytes fall outside the decision-open/close lease"
                    )
                if (
                    event.get("wire_token_sequence") != token.sequence
                    or event.get("wire_source_record_sequence")
                    != source.sequence
                ):
                    raise WireEvidenceError(
                        "action event does not name its exact raw token/source record"
                    )
            cursor = token.stream_end
        if any(byte not in (10, 13) for byte in stream[cursor:]):
            raise WireEvidenceError("captured client stream contains uncommitted bytes")

    first_preflop_by_connection: list[ParsedWireRecord | None] = [None, None]
    for record in server_records_global:
        if record.payload.startswith(b"preflop|"):
            first_preflop_by_connection[record.connection_index] = (
                first_preflop_by_connection[record.connection_index] or record
            )
    for connection in range(2):
        name_token = tokens_by_connection[connection][0]
        name_source = records[name_token.source_record_sequence]
        preflop = first_preflop_by_connection[connection]
        name_query = server_records_global[connection]
        if preflop is None or not (
            name_query.sequence < name_source.sequence < preflop.sequence
        ):
            raise WireEvidenceError(
                "client name bytes fall outside the name-query handshake lease"
            )

    inbound_by_connection = [
        [record for record in records if record.direction == "bot_to_server" and record.connection_index == connection]
        for connection in range(2)
    ]
    for decision_index, decision in enumerate(expected_decisions):
        open_record = server_records_global[decision.open_server_ordinal]
        close_record = server_records_global[decision.peer_relay_server_ordinal]
        request = decision.request_event
        action = decision.action_event
        if (
            request.get("decision_open_record_sequence") != open_record.sequence
            or request.get("requested_epoch_ms") != open_record.epoch_ms
            or request.get("requested_monotonic_ns") != open_record.monotonic_ns
        ):
            raise WireEvidenceError(
                "decision request timestamp is not derived from its server-write boundary"
            )
        if decision.bot_token_index is None:
            projected_fault = _NO_CLIENT_TOKEN_FAULT_ACTIONS.get(
                action.get("action")
            )
            if projected_fault is None:
                raise WireEvidenceError(
                    "decision without a client token lacks an explicit fault outcome"
                )
            if any(
                open_record.sequence < record.sequence < close_record.sequence
                for record in inbound_by_connection[decision.connection_index]
            ):
                raise WireEvidenceError(
                    "tokenless fault lease contains unsolicited client bytes"
                )
            if close_record.payload != b"fold\n":
                raise WireEvidenceError(
                    "tokenless fault is not closed by the peer fold relay"
                )
            platform_timeout_ms = request.get("platform_action_timeout_ms")
            action_epoch_ms = action.get("action_epoch_ms")
            action_monotonic_ns = action.get("action_monotonic_ns")
            if (
                type(platform_timeout_ms) is not int
                or type(action_epoch_ms) is not int
                or type(action_monotonic_ns) is not int
                or not open_record.epoch_ms
                <= action_epoch_ms
                <= close_record.epoch_ms
                or not open_record.monotonic_ns
                <= action_monotonic_ns
                <= close_record.monotonic_ns
            ):
                raise WireEvidenceError(
                    "tokenless fault timing is not bounded by raw causal records"
                )
            if projected_fault == "timeout":
                if (
                    action_monotonic_ns
                    < open_record.monotonic_ns + platform_timeout_ms * 1_000_000
                ):
                    raise WireEvidenceError(
                        "timeout timing is not bounded by the platform deadline"
                    )
            elif close_record.monotonic_ns >= (
                open_record.monotonic_ns + platform_timeout_ms * 1_000_000
            ):
                raise WireEvidenceError(
                    "tokenless non-timeout fault reached the platform deadline; "
                    "timeout attribution takes precedence"
                )
        else:
            token = tokens_by_connection[decision.connection_index][
                decision.bot_token_index
            ]
            if action.get("wire_token_sequence") != token.sequence:
                raise WireEvidenceError(
                    f"decision {decision_index} points at a different client token"
                )

    semantic_payload = {
        "schema": "pok-native-wire-semantic-binding-v3",
        "raw_wire_digest": hashlib.sha256(raw_wire).hexdigest(),
        "record_digests": [
            hashlib.sha256(record.payload).hexdigest() for record in records
        ],
        "server_messages": [list(row) for row in expected_server],
        "decision_edges": [
            {
                "connection_index": item.connection_index,
                "open_record_sequence": server_records_global[
                    item.open_server_ordinal
                ].sequence,
                "close_record_sequence": server_records_global[
                    item.peer_relay_server_ordinal
                ].sequence,
                "bot_token_index": item.bot_token_index,
            }
            for item in expected_decisions
        ],
        "bot_tokens": [
            [
                {
                    "message_type": item.message_type,
                    "payload": item.payload,
                    "completed_monotonic_ns": token.completed_monotonic_ns,
                }
                for item, token in zip(expected_bot[connection], tokens_by_connection[connection], strict=True)
            ]
            for connection in range(2)
        ],
    }
    return VerifiedWireSemantics(
        semantic_binding_digest=_digest_payload(semantic_payload),
        record_count=len(records),
        token_count=len(tokens),
        server_message_count_by_connection=(
            len(server_records[0]),
            len(server_records[1]),
        ),
        bot_token_count_by_connection=(
            len(tokens_by_connection[0]),
            len(tokens_by_connection[1]),
        ),
    )


_MISSING_EVENT_FIELD = object()


def _decision_event_field(event: object, name: str, index: int) -> Any:
    if isinstance(event, Mapping):
        value = event.get(name, _MISSING_EVENT_FIELD)
    else:
        value = getattr(event, name, _MISSING_EVENT_FIELD)
    if value is _MISSING_EVENT_FIELD:
        raise WireEvidenceError(
            f"decision enforcement event {index} lacks field {name!r}"
        )
    return value


def _decision_event_integer(event: object, name: str, index: int) -> int:
    value = _decision_event_field(event, name, index)
    if type(value) is not int or value < 0:
        raise WireEvidenceError(
            f"decision enforcement event {index} field {name!r} "
            "must be a non-negative integer"
        )
    return value


def _decision_event_digest(event: object, name: str, index: int) -> str:
    value = _decision_event_field(event, name, index)
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise WireEvidenceError(
            f"decision enforcement event {index} field {name!r} "
            "must be a canonical SHA-256 digest"
        )
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise WireEvidenceError(
            f"decision enforcement event {index} field {name!r} "
            "must be hexadecimal"
        ) from exc
    if len(decoded) != 32:
        raise WireEvidenceError(
            f"decision enforcement event {index} field {name!r} "
            "must be a 32-byte digest"
        )
    return value


def _resource_canonical_digest(value: Any) -> str:
    """Mirror the externally frozen resource-event canonical JSON digest."""

    return hashlib.sha256(_canonical_bytes(value) + b"\n").hexdigest()


def verify_decision_enforcement_events(
    raw_wire: bytes,
    replay_payload: Mapping[str, Any],
    decision_events: Sequence[object],
) -> VerifiedDecisionEnforcementBinding:
    """Bind every supervisor decision event to v3 wire and replay facts.

    ``DecisionEnforcementEvent`` is intentionally consumed by structural
    typing (attributes or mapping keys) so this evidence layer does not import
    the resource authority module.  Event ``decision_index`` is the global
    replay-decision order.  The replay action's own ``decision_index`` remains
    the native replay contract's per-connection counter and is independently
    checked here.

    Token digests have one exact meaning: the request digest hashes the whole
    actor-enabling ``server_to_bot`` record payload, including its single LF;
    the action digest hashes only the parser-committed client action token span,
    excluding any CR/LF separators.  Both use SHA-256 over those exact bytes.

    A tokenless crash, timeout, resource, protocol, or infrastructure fault has
    no client action token or completing ingress record.  The v3 resource event
    represents all three action-token fields as explicit nulls and closes the
    interval at the server's peer ``fold`` relay.  That relay is never accepted
    as a synthetic client token.
    """

    verified_wire = verify_structured_wire_capture(raw_wire, replay_payload)
    envelope = _strict_load(raw_wire)
    records = _parse_records(envelope["records"])
    tokens = _parse_tokens(envelope["tokens"])
    _expected_server, _expected_bot, expected_decisions = _expected_projection(
        replay_payload
    )
    if not expected_decisions:
        raise WireEvidenceError("replay contains no decisions to bind")
    if isinstance(decision_events, (str, bytes, bytearray, Mapping)):
        raise WireEvidenceError("decision enforcement trace must be an ordered sequence")
    try:
        materialized_events = tuple(decision_events)
    except TypeError as exc:
        raise WireEvidenceError(
            "decision enforcement trace must be an ordered sequence"
        ) from exc
    if len(materialized_events) != len(expected_decisions):
        raise WireEvidenceError(
            "decision enforcement trace does not cover every replay decision exactly once"
        )

    server_records_global = tuple(
        record for record in records if record.direction == "server_to_bot"
    )
    inbound_streams = [bytearray(), bytearray()]
    for record in records:
        if record.direction == "bot_to_server":
            inbound_streams[record.connection_index].extend(record.payload)
    tokens_by_connection: list[list[ParsedWireToken]] = [[], []]
    for token in tokens:
        tokens_by_connection[token.connection_index].append(token)

    capture_session_digest: str | None = None
    per_connection_decision_index = [0, 0]
    bound_rows: list[dict[str, Any]] = []
    for global_index, (decision, event) in enumerate(
        zip(expected_decisions, materialized_events, strict=True)
    ):
        request = decision.request_event
        action = decision.action_event
        connection = decision.connection_index
        hand = request.get("hand")
        street = request.get("stage")
        if (
            type(hand) is not int
            or not 1 <= hand <= 70
            or street not in {"preflop", "flop", "turn", "river"}
            or action.get("player_idx") != connection
            or action.get("hand") != hand
            or action.get("stage") != street
        ):
            raise WireEvidenceError(
                f"replay decision {global_index} request/action identity is inconsistent"
            )
        replay_local_index = action.get("decision_index")
        if (
            type(replay_local_index) is not int
            or replay_local_index != per_connection_decision_index[connection]
        ):
            raise WireEvidenceError(
                f"replay decision {global_index} has a non-contiguous actor-local index"
            )
        per_connection_decision_index[connection] += 1

        event_schema = _decision_event_field(event, "schema", global_index)
        if event_schema != DECISION_ENFORCEMENT_EVENT_SCHEMA:
            raise WireEvidenceError(
                f"decision enforcement event {global_index} is not the v3 schema"
            )
        try:
            open_record = server_records_global[decision.open_server_ordinal]
            close_record = server_records_global[decision.peer_relay_server_ordinal]
        except IndexError as exc:
            raise WireEvidenceError(
                f"replay decision {global_index} names unavailable wire evidence"
            ) from exc
        if (
            open_record.connection_index != connection
            or close_record.connection_index != 1 - connection
            or not open_record.sequence < close_record.sequence
        ):
            raise WireEvidenceError(
                f"replay decision {global_index} open/close records are not an "
                "actor request and peer relay"
            )
        expected_request_digest = hashlib.sha256(open_record.payload).hexdigest()

        event_global_index = _decision_event_integer(
            event, "decision_index", global_index
        )
        event_connection = _decision_event_integer(
            event, "connection_index", global_index
        )
        event_hand_index = _decision_event_integer(event, "hand_index", global_index)
        event_street = _decision_event_field(event, "street", global_index)
        if (
            event_global_index != global_index
            or event_connection != connection
            or event_hand_index != hand - 1
            or event_street != street
        ):
            raise WireEvidenceError(
                f"decision enforcement event {global_index} identity/order "
                "differs from replay"
            )

        event_request_sequence = _decision_event_integer(
            event, "request_raw_record_seq", global_index
        )
        event_close_sequence = _decision_event_integer(
            event, "decision_close_raw_record_seq", global_index
        )
        if event_request_sequence != open_record.sequence:
            raise WireEvidenceError(
                f"decision enforcement event {global_index} request raw record "
                "is not its decision-open server record"
            )
        if event_close_sequence != close_record.sequence:
            raise WireEvidenceError(
                f"decision enforcement event {global_index} close raw record "
                "is not its server peer-relay record"
            )

        event_request_digest = _decision_event_digest(
            event, "request_token_digest", global_index
        )
        if event_request_digest != expected_request_digest:
            raise WireEvidenceError(
                f"decision enforcement event {global_index} request token digest "
                "does not hash the exact decision-open server bytes"
            )

        requested_monotonic_ns = _decision_event_integer(
            event, "requested_monotonic_ns", global_index
        )
        decision_close_monotonic_ns = _decision_event_integer(
            event, "decision_close_monotonic_ns", global_index
        )
        if (
            requested_monotonic_ns != open_record.monotonic_ns
            or request.get("requested_monotonic_ns") != open_record.monotonic_ns
        ):
            raise WireEvidenceError(
                f"decision enforcement event {global_index} request timestamp "
                "is not the server-write boundary"
            )
        if decision_close_monotonic_ns != close_record.monotonic_ns:
            raise WireEvidenceError(
                f"decision enforcement event {global_index} close timestamp "
                "is not the server peer-relay boundary"
            )

        event_fault_kind = _decision_event_field(event, "fault_kind", global_index)
        event_action_sequence_value = _decision_event_field(
            event, "action_raw_record_seq", global_index
        )
        event_action_digest_value = _decision_event_field(
            event, "action_token_digest", global_index
        )
        event_action_time_value = _decision_event_field(
            event, "action_sent_monotonic_ns", global_index
        )
        projected_no_token_fault = _NO_CLIENT_TOKEN_FAULT_ACTIONS.get(
            action.get("action")
        )
        has_no_client_token = decision.bot_token_index is None
        if has_no_client_token != (projected_no_token_fault is not None):
            raise WireEvidenceError(
                f"replay decision {global_index} fault/token projection is inconsistent"
            )

        token: ParsedWireToken | None = None
        event_action_sequence: int | None = None
        event_action_digest: str | None = None
        action_sent_monotonic_ns: int | None = None
        action_token_span: list[int] | None = None
        if has_no_client_token:
            if event_fault_kind != projected_no_token_fault:
                raise WireEvidenceError(
                    f"decision enforcement event {global_index} does not classify "
                    "the replay tokenless fault"
                )
            if any(
                value is not None
                for value in (
                    event_action_sequence_value,
                    event_action_digest_value,
                    event_action_time_value,
                )
            ):
                raise WireEvidenceError(
                    f"decision enforcement event {global_index} tokenless fault fabricates "
                    "a client action token or ingress"
                )
            if close_record.payload != b"fold\n":
                raise WireEvidenceError(
                    f"decision enforcement event {global_index} tokenless fault does not "
                    "close at the peer fold adjudication relay"
                )
        else:
            if event_fault_kind == "timeout":
                raise WireEvidenceError(
                    f"decision enforcement event {global_index} misclassifies a "
                    "client action as timeout"
                )
            if any(
                value is None
                for value in (
                    event_action_sequence_value,
                    event_action_digest_value,
                    event_action_time_value,
                )
            ):
                raise WireEvidenceError(
                    f"decision enforcement event {global_index} non-timeout lacks "
                    "its client action token or ingress"
                )
            event_action_sequence = _decision_event_integer(
                event, "action_raw_record_seq", global_index
            )
            event_action_digest = _decision_event_digest(
                event, "action_token_digest", global_index
            )
            action_sent_monotonic_ns = _decision_event_integer(
                event, "action_sent_monotonic_ns", global_index
            )
            try:
                token = tokens_by_connection[connection][decision.bot_token_index]
                source_record = records[token.source_record_sequence]
            except IndexError as exc:
                raise WireEvidenceError(
                    f"replay decision {global_index} names unavailable action evidence"
                ) from exc
            if (
                token.message_type != "action"
                or source_record.direction != "bot_to_server"
                or source_record.connection_index != connection
                or not (
                    open_record.sequence
                    < token.source_record_sequence
                    < close_record.sequence
                )
            ):
                raise WireEvidenceError(
                    f"replay decision {global_index} action token does not complete "
                    "strictly inside its open/close interval"
                )
            if event_action_sequence != token.source_record_sequence:
                raise WireEvidenceError(
                    f"decision enforcement event {global_index} action raw record "
                    "is not its token-completing ingress record"
                )
            action_token = bytes(inbound_streams[connection])[
                token.stream_start : token.stream_end
            ]
            expected_action_digest = hashlib.sha256(action_token).hexdigest()
            if event_action_digest != expected_action_digest:
                raise WireEvidenceError(
                    f"decision enforcement event {global_index} action token digest "
                    "does not hash the exact committed client token bytes"
                )
            if (
                action_sent_monotonic_ns != token.completed_monotonic_ns
                or action.get("action_monotonic_ns") != token.completed_monotonic_ns
                or source_record.monotonic_ns != token.completed_monotonic_ns
            ):
                raise WireEvidenceError(
                    f"decision enforcement event {global_index} action timestamp "
                    "is not the token-completing ingress boundary"
                )
            action_token_span = [token.stream_start, token.stream_end]

        event_capture_session = _decision_event_digest(
            event, "capture_session_digest", global_index
        )
        if capture_session_digest is None:
            capture_session_digest = event_capture_session
        elif event_capture_session != capture_session_digest:
            raise WireEvidenceError(
                "decision enforcement trace crosses supervisor capture sessions"
            )
        event_decision_id = _decision_event_digest(event, "decision_id", global_index)
        expected_decision_id = _resource_canonical_digest(
            {
                "action_raw_record_seq": event_action_sequence,
                "action_token_digest": event_action_digest,
                "capture_session_digest": event_capture_session,
                "connection_index": event_connection,
                "decision_close_raw_record_seq": event_close_sequence,
                "decision_index": event_global_index,
                "hand_index": event_hand_index,
                "request_raw_record_seq": event_request_sequence,
                "schema": DECISION_IDENTITY_SCHEMA,
                "street": event_street,
            }
        )
        if event_decision_id != expected_decision_id:
            raise WireEvidenceError(
                f"decision enforcement event {global_index} decision ID is stale"
            )

        bound_rows.append(
            {
                "action_raw_record_sequence": event_action_sequence,
                "action_token_digest": event_action_digest,
                "action_token_sequence": None if token is None else token.sequence,
                "action_token_stream_span": action_token_span,
                "action_timestamp_monotonic_ns": action_sent_monotonic_ns,
                "connection_index": connection,
                "decision_close_raw_record_sequence": event_close_sequence,
                "decision_close_timestamp_monotonic_ns": decision_close_monotonic_ns,
                "decision_id": event_decision_id,
                "decision_index": global_index,
                "fault_kind": event_fault_kind,
                "has_no_client_token": has_no_client_token,
                "is_timeout": projected_no_token_fault == "timeout",
                "hand_index": hand - 1,
                "request_raw_record_sequence": event_request_sequence,
                "request_timestamp_monotonic_ns": requested_monotonic_ns,
                "request_token_digest": event_request_digest,
                "street": street,
            }
        )

    if capture_session_digest is None:
        raise WireEvidenceError("decision enforcement trace has no capture session")
    binding_payload = {
        "action_token_digest_spec": DECISION_ACTION_TOKEN_DIGEST_SPEC,
        "capture_session_digest": capture_session_digest,
        "decision_rows": bound_rows,
        "raw_wire_digest": hashlib.sha256(raw_wire).hexdigest(),
        "replay_projection_digest": _digest_payload(replay_payload),
        "request_token_digest_spec": DECISION_REQUEST_TOKEN_DIGEST_SPEC,
        "schema": "pok-native-decision-enforcement-binding-v3",
        "wire_semantic_binding_digest": verified_wire.semantic_binding_digest,
    }
    return VerifiedDecisionEnforcementBinding(
        binding_digest=_digest_payload(binding_payload),
        capture_session_digest=capture_session_digest,
        decision_count=len(bound_rows),
    )


__all__ = [
    "DECISION_ACTION_TOKEN_DIGEST_SPEC",
    "DECISION_ENFORCEMENT_EVENT_SCHEMA",
    "DECISION_IDENTITY_SCHEMA",
    "DECISION_REQUEST_TOKEN_DIGEST_SPEC",
    "MAX_WIRE_CAPTURE_BYTES",
    "ParsedWireRecord",
    "ParsedWireToken",
    "VerifiedWireSemantics",
    "VerifiedDecisionEnforcementBinding",
    "WIRE_CAPTURE_FRAMING",
    "WIRE_CAPTURE_SCHEMA",
    "WireEvidenceError",
    "is_structured_wire_capture",
    "verify_decision_enforcement_events",
    "verify_structured_wire_capture",
]
