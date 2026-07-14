"""Fail-closed native-TCP capture harness for the common replay contract.

The existing production evolution runner is intentionally not treated as a
formal evidence producer: it accepts a scalar deck seed, records best-effort
text observability, and does not bind concrete executions.  This module is a
development reference path.  It consumes the exact frozen 70-deck window,
records raw bytes with ordered monotonic timestamps, emits the strict replay
schema, and immediately verifies the result.

``run_development_tcp_capture`` is diagnostic-only because its two clients are
not launched by the privileged resource supervisor.  A formal producer must
use the same capture/verification path with an enforcer-bound
``ReplayExecutionBinding`` plus trusted worker telemetry; it must never
relabel this development result.  Server-observed arrival timing is real, but
search nodes, fallback choice, compute completion, and official send throttle
remain explicitly unavailable here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any, Sequence

from sever.engine.deck import Card, Deck
from sever.engine.game import GameEngine
from web.core.national_transport import (
    MAX_CLIENT_BUFFER_BYTES,
    NationalProtocolError,
    NationalTCPClient,
)

from .deal_generator import (
    DealWindowCommitment,
    generate_tcp_deck,
    tcp_card_from_id,
)
from .native_replay import (
    ReplayExecutionBinding,
    VerifiedNativeReplay,
    verify_native_replay,
)
from .native_wire import WIRE_CAPTURE_FRAMING, WIRE_CAPTURE_SCHEMA


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RawWireRecord:
    sequence: int
    connection_index: int
    direction: str
    epoch_ms: int
    monotonic_ns: int
    payload_hex: str


@dataclass(frozen=True, slots=True)
class RawWireToken:
    sequence: int
    connection_index: int
    message_type: str
    stream_start: int
    stream_end: int
    source_record_sequence: int
    completed_epoch_ms: int
    completed_monotonic_ns: int


class StrictRawWireRecorder:
    """In-memory reference recorder; every callback is synchronous/fatal."""

    def __init__(self) -> None:
        self._records: list[RawWireRecord] = []
        self._tokens: list[RawWireToken] = []
        self._inbound_streams = [bytearray(), bytearray()]
        self._inbound_ranges: list[list[tuple[int, int, int]]] = [[], []]
        self._token_cursors = [0, 0]

    def record(
        self,
        connection_index: int,
        direction: str,
        payload: bytes,
    ) -> RawWireRecord:
        if type(connection_index) is not int or connection_index not in (0, 1):
            raise ValueError("wire recorder requires connection index 0 or 1")
        if direction not in {"server_to_bot", "bot_to_server"}:
            raise ValueError("unknown wire direction")
        if type(payload) is not bytes or not payload:
            raise ValueError("wire recorder requires non-empty raw bytes")
        record = RawWireRecord(
            sequence=len(self._records),
            connection_index=connection_index,
            direction=direction,
            epoch_ms=time.time_ns() // 1_000_000,
            monotonic_ns=time.monotonic_ns(),
            payload_hex=payload.hex(),
        )
        self._records.append(record)
        if direction == "bot_to_server":
            stream = self._inbound_streams[connection_index]
            start = len(stream)
            stream.extend(payload)
            self._inbound_ranges[connection_index].append(
                (start, len(stream), record.sequence)
            )
        return record

    def commit_token(
        self,
        connection_index: int,
        message_type: str,
        payload: str,
    ) -> RawWireToken:
        """Bind a parser-committed token to its exact inbound byte span."""

        if connection_index not in (0, 1):
            raise ValueError("token connection must be 0 or 1")
        if message_type not in {"name", "action"}:
            raise ValueError("unknown committed token type")
        if not isinstance(payload, str) or not payload:
            raise ValueError("committed token text must be non-empty")
        expected = payload.encode("utf-8", errors="strict")
        stream = bytes(self._inbound_streams[connection_index])
        cursor = self._token_cursors[connection_index]
        while cursor < len(stream) and stream[cursor] in (10, 13):
            cursor += 1
        start = cursor
        end = start + len(expected)
        if stream[start:end] != expected:
            raise RuntimeError(
                "parser token is not an exact slice of the captured client stream"
            )
        source_record_sequence = next(
            (
                sequence
                for range_start, range_end, sequence in self._inbound_ranges[
                    connection_index
                ]
                if range_start < end <= range_end
            ),
            None,
        )
        if source_record_sequence is None:
            raise RuntimeError("committed token lacks a completing raw record")
        source_record = self._records[source_record_sequence]
        token = RawWireToken(
            sequence=len(self._tokens),
            connection_index=connection_index,
            message_type=message_type,
            stream_start=start,
            stream_end=end,
            source_record_sequence=source_record_sequence,
            completed_epoch_ms=source_record.epoch_ms,
            completed_monotonic_ns=source_record.monotonic_ns,
        )
        self._tokens.append(token)
        self._token_cursors[connection_index] = end
        return token

    @property
    def records(self) -> tuple[RawWireRecord, ...]:
        return tuple(self._records)

    @property
    def tokens(self) -> tuple[RawWireToken, ...]:
        return tuple(self._tokens)

    def _assert_fully_committed(self) -> None:
        for connection in range(2):
            stream = self._inbound_streams[connection]
            cursor = self._token_cursors[connection]
            if any(byte not in (10, 13) for byte in stream[cursor:]):
                raise RuntimeError(
                    f"connection {connection} has uncommitted inbound TCP bytes"
                )

    def canonical_bytes(self) -> bytes:
        self._assert_fully_committed()
        return _canonical_bytes(
            {
                "schema": WIRE_CAPTURE_SCHEMA,
                "framing": WIRE_CAPTURE_FRAMING,
                "records": [
                    {
                        "sequence": item.sequence,
                        "connection_index": item.connection_index,
                        "direction": item.direction,
                        "epoch_ms": item.epoch_ms,
                        "monotonic_ns": item.monotonic_ns,
                        "payload_hex": item.payload_hex,
                    }
                    for item in self._records
                ],
                "tokens": [
                    {
                        "sequence": item.sequence,
                        "connection_index": item.connection_index,
                        "message_type": item.message_type,
                        "stream_start": item.stream_start,
                        "stream_end": item.stream_end,
                        "source_record_sequence": item.source_record_sequence,
                        "completed_epoch_ms": item.completed_epoch_ms,
                        "completed_monotonic_ns": item.completed_monotonic_ns,
                    }
                    for item in self._tokens
                ],
            }
        )


class StrictRecordingClient(NationalTCPClient):
    """National client whose raw-byte recorder cannot fail silently."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        connection_index: int,
        recorder: StrictRawWireRecorder,
        idle_flush_sec: float = 0.001,
    ) -> None:
        super().__init__(
            reader,
            writer,
            idle_flush_sec=idle_flush_sec,
            max_buffer_bytes=MAX_CLIENT_BUFFER_BYTES,
            wire_sink=None,
        )
        self._connection_index = connection_index
        self._strict_recorder = recorder
        self.last_action_completion_epoch_ms: int | None = None
        self.last_action_completion_monotonic_ns: int | None = None
        self.last_action_token: RawWireToken | None = None
        self.last_server_record: RawWireRecord | None = None

    async def _notify(self, **event: Any) -> None:
        # Semantic notifications in the legacy transport are best-effort text.
        # Raw bytes are captured directly in ``send_line``/``_read_chunk``.
        return None

    async def _finish_name(self, name: str) -> str:
        result = await super()._finish_name(name)
        self._strict_recorder.commit_token(
            self._connection_index,
            "name",
            result,
        )
        return result

    async def send_line(self, message: str) -> None:
        if self.closed:
            raise ConnectionError("strict transport attempted to send to a closed client")
        payload = (message + "\n").encode("utf-8")
        # Publish the conservative decision-open timestamp before the bytes
        # enter the transport.  Recording only after ``drain`` would grant an
        # unmetered compute window to a client that receives them meanwhile.
        record = self._strict_recorder.record(
            self._connection_index,
            "server_to_bot",
            payload,
        )
        self.writer.write(payload)
        await self.writer.drain()
        self.last_server_record = record

    async def _read_chunk(self, timeout: float) -> bool:
        try:
            chunk = await asyncio.wait_for(self.reader.read(4096), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        if not chunk:
            self.closed = True
            try:
                tail = self._decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise NationalProtocolError("client_invalid_utf8") from exc
            if tail:
                self._buffer += tail
            return False
        self._buffer_bytes += len(chunk)
        if self._buffer_bytes > self.max_buffer_bytes:
            raise NationalProtocolError("client_buffer_limit_exceeded")
        try:
            self._buffer += self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as exc:
            raise NationalProtocolError("client_invalid_utf8") from exc
        return True

    async def _finish_action(self, action: str, remainder: str) -> str:
        result = await super()._finish_action(action, remainder)
        token = self._strict_recorder.commit_token(
            self._connection_index,
            "action",
            result,
        )
        self.last_action_completion_epoch_ms = token.completed_epoch_ms
        self.last_action_completion_monotonic_ns = token.completed_monotonic_ns
        self.last_action_token = token
        return result


class StrictRecordingStreamReaderProtocol(asyncio.StreamReaderProtocol):
    """Capture ingress in ``data_received``, before the engine asks to read it."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        client_connected_cb: Any,
        *,
        connection_index: int,
        recorder: StrictRawWireRecorder,
    ) -> None:
        super().__init__(reader, client_connected_cb)
        self._connection_index = connection_index
        self._strict_recorder = recorder

    def data_received(self, data: bytes) -> None:
        self._strict_recorder.record(
            self._connection_index,
            "bot_to_server",
            bytes(data),
        )
        super().data_received(data)


def _exact_deck(seed: int) -> Deck:
    deck = Deck(seed=0)
    deck.cards = [
        Card(*tcp_card_from_id(card_id)) for card_id in generate_tcp_deck(seed)
    ]
    return deck


class CapturingNationalGameEngine(GameEngine):
    """National engine that materializes strict timing/deal evidence."""

    def __init__(
        self,
        clients: Sequence[StrictRecordingClient],
        *,
        hand_seeds: Sequence[int],
        decision_budget_ms: int,
        platform_action_timeout_ms: int,
        action_send_delay_ms: int,
    ) -> None:
        if len(clients) != 2 or len(hand_seeds) != 70:
            raise ValueError("capturing engine requires two clients and 70 hand seeds")
        self._clients = tuple(clients)
        self.events: list[dict[str, Any]] = []
        self._pending: dict[int, tuple[int, int]] = {}
        self._action_arrival: dict[int, tuple[int, int]] = {}
        self._decision_counts = [0, 0]
        self.decision_budget_ms = decision_budget_ms
        self.platform_action_timeout_ms = platform_action_timeout_ms
        self.action_send_delay_ms = action_send_delay_ms
        self.action_timeout_sec = platform_action_timeout_ms / 1000
        seeds = tuple(hand_seeds)
        super().__init__(
            send_func=self._send_to_client,
            broadcast_func=self._capture_event,
            recorder=None,
            deck_factory=lambda hand_number: _exact_deck(seeds[hand_number - 1]),
        )

    async def _send_to_client(self, player_idx: int, message: str) -> None:
        await self._clients[player_idx].send_line(message)

    async def _recv_action(self, player_idx: int) -> str | None:
        action = await self._clients[player_idx].recv_line(
            timeout=self.action_timeout_sec
        )
        client = self._clients[player_idx]
        if action is None:
            self._action_arrival[player_idx] = (
                time.time_ns() // 1_000_000,
                time.monotonic_ns(),
            )
        else:
            if (
                client.last_action_completion_epoch_ms is None
                or client.last_action_completion_monotonic_ns is None
            ):
                raise RuntimeError("completed action lacks strict token-boundary timing")
            self._action_arrival[player_idx] = (
                client.last_action_completion_epoch_ms,
                client.last_action_completion_monotonic_ns,
            )
        return action

    async def _capture_event(self, raw_event: dict[str, Any]) -> None:
        event = dict(raw_event)
        event_type = event.get("type")
        now_epoch_ms = time.time_ns() // 1_000_000
        now_monotonic_ns = time.monotonic_ns()
        if event_type == "action_requested":
            player_idx = int(event["player_idx"])
            boundary = self._clients[player_idx].last_server_record
            if boundary is None:
                raise RuntimeError("decision opens without an actor-enabling server write")
            now_epoch_ms = boundary.epoch_ms
            now_monotonic_ns = boundary.monotonic_ns
            event.update(
                {
                    "decision_open_record_sequence": boundary.sequence,
                    "timeout_budget_sec": 60.0,
                    "decision_budget_ms": self.decision_budget_ms,
                    "platform_action_timeout_ms": self.platform_action_timeout_ms,
                    "action_send_delay_ms": self.action_send_delay_ms,
                    "requested_epoch_ms": now_epoch_ms,
                    "compute_deadline_epoch_ms": (
                        now_epoch_ms + self.decision_budget_ms
                    ),
                    "deadline_epoch_ms": (
                        now_epoch_ms + self.platform_action_timeout_ms
                    ),
                    "requested_monotonic_ns": now_monotonic_ns,
                    "compute_deadline_monotonic_ns": (
                        now_monotonic_ns + self.decision_budget_ms * 1_000_000
                    ),
                    "platform_deadline_monotonic_ns": (
                        now_monotonic_ns
                        + self.platform_action_timeout_ms * 1_000_000
                    ),
                }
            )
            self._pending[player_idx] = (now_epoch_ms, now_monotonic_ns)
        elif event_type == "action":
            player_idx = int(event["player_idx"])
            if player_idx not in self._pending:
                raise RuntimeError("action event lacks a captured request")
            requested_epoch_ms, requested_monotonic_ns = self._pending.pop(player_idx)
            if player_idx not in self._action_arrival:
                raise RuntimeError("action event lacks token-boundary arrival timing")
            action_epoch_ms, action_monotonic_ns = self._action_arrival.pop(player_idx)
            wait_ns = action_monotonic_ns - requested_monotonic_ns
            if wait_ns < 0:
                raise RuntimeError("client action arrived before its decision-open boundary")
            wait_ms = wait_ns // 1_000_000
            is_timeout = event.get("action") == "timeout"
            action_fields: dict[str, Any] = {
                    "decision_index": self._decision_counts[player_idx],
                    "search_nodes": 0,
                    "fallback_used": False,
                    "snapshot_tier": "telemetry-unavailable",
                    "telemetry_source": "harness_arrival_only",
                    "decision_wait_sec": wait_ns / 1_000_000_000,
                    "decision_wait_ms": wait_ms,
                    "decision_wait_ns": wait_ns,
                    "timeout_budget_sec": 60.0,
                    "decision_budget_ms": self.decision_budget_ms,
                    "platform_action_timeout_ms": self.platform_action_timeout_ms,
                    "action_send_delay_ms": self.action_send_delay_ms,
                    "action_epoch_ms": max(action_epoch_ms, requested_epoch_ms),
                    "action_monotonic_ns": action_monotonic_ns,
            }
            if not is_timeout:
                token = self._clients[player_idx].last_action_token
                if token is None:
                    raise RuntimeError("action event lacks a raw-wire token binding")
                action_fields.update(
                    {
                        "wire_token_sequence": token.sequence,
                        "wire_source_record_sequence": (
                            token.source_record_sequence
                        ),
                    }
                )
            event.update(action_fields)
            if self.action_send_delay_ms:
                raise RuntimeError(
                    "server-only capture cannot attest a positive send delay"
                )
            self._decision_counts[player_idx] += 1
        elif event_type == "match_end":
            if int(event.get("hands_played", 0)) == 70:
                event["result_finalized_epoch_ms"] = now_epoch_ms
        self.events.append(event)

    async def run_limited_match(self, names: tuple[str, str]) -> None:
        self.players[0].name, self.players[1].name = names
        self.total_earnings = [0, 0]
        self.match_over = False
        for hand_number in range(1, 71):
            self.hand_num = hand_number
            result = await self._run_hand(hand_number)
            if result is None:
                break
            self.total_earnings[0] += result.earnings[0]
            self.total_earnings[1] += result.earnings[1]
            if self.match_over:
                break
        await self._emit(
            "match_end",
            {
                "total_earnings": list(self.total_earnings),
                "names": [player.name for player in self.players],
                "hands_played": self.hand_num,
            },
        )


@dataclass(frozen=True, slots=True)
class DevelopmentCaptureResult:
    raw_replay: bytes
    raw_wire: bytes
    execution_binding: ReplayExecutionBinding
    verified_replay: VerifiedNativeReplay


async def _send_test_action(
    writer: asyncio.StreamWriter,
    action: str,
    *,
    split: bool,
) -> None:
    payload = action.encode("ascii")
    if split and len(payload) > 1:
        midpoint = max(1, len(payload) // 2)
        writer.write(payload[:midpoint])
        await writer.drain()
        await asyncio.sleep(0.002)
        writer.write(payload[midpoint:])
    else:
        writer.write(payload)
    await writer.drain()


async def _scripted_test_bot(
    host: str,
    port: int,
    name: str,
    scenario: str,
) -> None:
    reader, writer = await asyncio.open_connection(host, port)
    role = ""
    street = ""
    allin_locked = False
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            message = line.decode("utf-8", errors="strict").rstrip("\r\n")
            if message == "name":
                writer.write((name + "\n").encode("utf-8"))
                await writer.drain()
            elif message.startswith("preflop|"):
                role = message.split("|", 2)[1]
                street = "preflop"
                allin_locked = False
                if role == "SMALLBLIND":
                    first_action = {
                        "fold": "fold",
                        "checkdown": "call",
                        "allin": "allin",
                        "minraise_split": "raise 200",
                    }[scenario]
                    await _send_test_action(
                        writer,
                        first_action,
                        split=scenario == "minraise_split",
                    )
                    if scenario == "allin":
                        allin_locked = True
            elif message.startswith(("flop|", "turn|", "river|")):
                street = message.split("|", 1)[0]
                if role == "BIGBLIND" and not allin_locked:
                    await _send_test_action(writer, "check", split=False)
            elif message == "call" and street == "preflop" and role == "BIGBLIND":
                await _send_test_action(writer, "check", split=False)
            elif message == "allin" and role == "BIGBLIND":
                await _send_test_action(writer, "call", split=False)
                allin_locked = True
            elif message == "raise 200" and street == "preflop" and role == "BIGBLIND":
                await _send_test_action(writer, "call", split=False)
            elif message == "check" and street != "preflop" and role == "SMALLBLIND":
                await _send_test_action(writer, "call", split=False)
    finally:
        writer.close()
        await writer.wait_closed()


def _client_order_event(
    binding: ReplayExecutionBinding,
    names: tuple[str, str],
) -> dict[str, Any]:
    return {
        "type": "client_order",
        "order": list(names),
        "connection_order": list(names),
        "connection_identity_digests": list(
            binding.connection_identity_digests
        ),
        "run_ids_by_connection": list(binding.run_ids_by_connection),
        "process_tree_ids_by_connection": list(
            binding.process_tree_ids_by_connection
        ),
        "cgroup_paths_by_connection": list(binding.cgroup_paths_by_connection),
        "connection_binding_digests": list(
            binding.connection_binding_digests()
        ),
    }


def _payload_from_events(
    *,
    events: list[dict[str, Any]],
    names: tuple[str, str],
    binding: ReplayExecutionBinding,
) -> bytes:
    settlements = [
        {
            key: event.get(key, "" if key == "reason" else None)
            for key in (
                "hand",
                "earnings",
                "pot",
                "is_showdown",
                "winner_idx",
                "reason",
            )
        }
        for event in events
        if event.get("type") == "settle"
    ]
    totals = [
        sum(int(row["earnings"][connection]) for row in settlements)
        for connection in range(2)
    ]
    timeout_counts = [
        sum(
            1
            for event in events
            if event.get("type") == "action"
            and event.get("player_idx") == connection
            and event.get("action") == "timeout"
        )
        for connection in range(2)
    ]
    illegal_counts = [
        sum(
            1
            for event in events
            if event.get("type") == "action"
            and event.get("player_idx") == connection
            and str(event.get("action", "")).startswith("illegal:")
        )
        for connection in range(2)
    ]
    issues: list[str] = []
    for connection, name in enumerate(names):
        if timeout_counts[connection]:
            issues.append(f"{name}: timeouts={timeout_counts[connection]}")
        if illegal_counts[connection]:
            issues.append(f"{name}: illegal_actions={illegal_counts[connection]}")
    per_player = {
        name: {
            "earnings": totals[connection],
            "illegal_actions": illegal_counts[connection],
            "timeouts": timeout_counts[connection],
            "wrapper_used": False,
            "passed_compliance": not any(
                issue.startswith(f"{name}:") for issue in issues
            ),
            "compliance_issues": [
                issue for issue in issues if issue.startswith(f"{name}:")
            ],
            "native": {
                "returncode": 0,
                "process_failures": 0,
                "json_response_stdout": 0,
            },
        }
        for connection, name in enumerate(names)
    }
    payload = {
        "execution_binding": binding.capture_payload(),
        "bot_a": names[0],
        "bot_b": names[1],
        "hands_requested": 70,
        "hands_played": len(settlements),
        "execution_mode": "native_tcp",
        "wrapper_used": False,
        "wrapper_used_by_player": {names[0]: False, names[1]: False},
        "per_player": per_player,
        "net_chips_a": totals[0],
        "net_chips_b": totals[1],
        "settlements": settlements,
        "passed_compliance": not issues,
        "issues": issues,
        "events_tail": events[-20:],
        "events": events,
    }
    return _canonical_bytes(payload)


async def run_development_tcp_capture(
    commitment: DealWindowCommitment,
    *,
    decision_budget_ms: int = 250,
    scenario: str = "fold",
) -> DevelopmentCaptureResult:
    """Run one real-socket 70-hand diagnostic capture with scripted bots."""

    if not isinstance(commitment, DealWindowCommitment):
        raise TypeError("development capture requires a DealWindowCommitment")
    if scenario not in {"fold", "checkdown", "allin", "minraise_split"}:
        raise ValueError("unknown development native-TCP scenario")
    names = ("CommonHarnessA", "CommonHarnessB")
    recorder = StrictRawWireRecorder()
    clients: list[StrictRecordingClient] = []
    connected = asyncio.Event()

    def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(clients) >= 2:
            writer.close()
            return
        clients.append(
            StrictRecordingClient(
                reader,
                writer,
                connection_index=len(clients),
                recorder=recorder,
            )
        )
        if len(clients) == 2:
            connected.set()

    loop = asyncio.get_running_loop()
    next_connection_index = 0

    def protocol_factory() -> StrictRecordingStreamReaderProtocol:
        nonlocal next_connection_index
        connection_index = next_connection_index
        next_connection_index += 1
        reader = asyncio.StreamReader(limit=MAX_CLIENT_BUFFER_BYTES)
        return StrictRecordingStreamReaderProtocol(
            reader,
            handle,
            connection_index=connection_index,
            recorder=recorder,
        )

    server = await loop.create_server(protocol_factory, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    tasks: list[asyncio.Task[None]] = []
    try:
        tasks.append(
            asyncio.create_task(
                _scripted_test_bot(str(host), int(port), names[0], scenario)
            )
        )
        while len(clients) < 1:
            await asyncio.sleep(0)
        tasks.append(
            asyncio.create_task(
                _scripted_test_bot(str(host), int(port), names[1], scenario)
            )
        )
        await asyncio.wait_for(connected.wait(), timeout=5.0)
        for index, client in enumerate(clients):
            await client.send_line("name")
            observed_name = await client.recv_name(timeout=5.0)
            if observed_name != names[index]:
                raise RuntimeError("test client name/connection mapping changed")
            client.name = observed_name

        engine = CapturingNationalGameEngine(
            clients,
            hand_seeds=commitment.hand_seeds,
            decision_budget_ms=decision_budget_ms,
            platform_action_timeout_ms=60_000,
            action_send_delay_ms=0,
        )
        await asyncio.wait_for(engine.run_limited_match(names), timeout=30.0)
        raw_wire = recorder.canonical_bytes()
        binding = ReplayExecutionBinding.for_development(
            leg_plan_digest=_digest("common-harness-development-leg"),
            connection_identity_digests=(
                _digest("common-harness-development-identity-0"),
                _digest("common-harness-development-identity-1"),
            ),
            run_ids_by_connection=(
                _digest("common-harness-development-run-0"),
                _digest("common-harness-development-run-1"),
            ),
            process_tree_ids_by_connection=(
                "development-in-process-client-0",
                "development-in-process-client-1",
            ),
            cgroup_paths_by_connection=(
                "/sys/fs/cgroup/pok-development/common-harness/0",
                "/sys/fs/cgroup/pok-development/common-harness/1",
            ),
            resource_profile_digest=_digest(
                f"common-harness-development-profile-{decision_budget_ms}"
            ),
            decision_budget_ms=decision_budget_ms,
            platform_action_timeout_ms=60_000,
            action_send_delay_ms=0,
            raw_wire=raw_wire,
        )
        events = [_client_order_event(binding, names), *engine.events]
        raw_replay = _payload_from_events(
            events=events,
            names=names,
            binding=binding,
        )
        verified = verify_native_replay(
            raw_replay,
            commitment,
            execution_binding=binding,
            raw_wire=raw_wire,
        )
        return DevelopmentCaptureResult(
            raw_replay=raw_replay,
            raw_wire=raw_wire,
            execution_binding=binding,
            verified_replay=verified,
        )
    finally:
        server.close()
        for client in clients:
            await client.close(timeout=1.0)
        await server.wait_closed()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def run_development_tcp_capture_sync(
    commitment: DealWindowCommitment,
    *,
    decision_budget_ms: int = 250,
    scenario: str = "fold",
) -> DevelopmentCaptureResult:
    return asyncio.run(
        run_development_tcp_capture(
            commitment,
            decision_budget_ms=decision_budget_ms,
            scenario=scenario,
        )
    )


__all__ = [
    "DevelopmentCaptureResult",
    "RawWireRecord",
    "RawWireToken",
    "StrictRawWireRecorder",
    "run_development_tcp_capture",
    "run_development_tcp_capture_sync",
]
