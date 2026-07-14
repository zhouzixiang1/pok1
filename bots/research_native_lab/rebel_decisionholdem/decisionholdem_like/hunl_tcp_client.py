"""Route-owned raw-socket client for the Common HUNL blueprint runtime."""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ...common_contracts.constants import OFFICIAL_ACTION_DELAY_SEC
from .common_native_entry import CommonA2StrategyRuntime
from .hunl_blueprint import (
    HUNL_FALLBACK_CONTRACT,
    HUNL_MATERIAL_POLICY_L1_THRESHOLD,
    HUNLBlueprint,
)
from .secure_files import atomic_json_write


TCP_CLIENT_SCHEMA = "route-a2-hunl-tcp-client-telemetry-v4"
OFFICIAL_RAW_FRAMING = "official-raw-no-delimiter"
SEVER_LINE_FRAMING = "sever-local-line-adapter"


def encode_client_message(message: str, framing: str) -> bytes:
    """Encode one already-authorized send without weakening official framing."""

    if type(message) is not str or not message or any(c in message for c in "\r\n"):
        raise ValueError("outgoing national message must be non-empty and line-free")
    if framing == OFFICIAL_RAW_FRAMING:
        return message.encode("ascii")
    if framing == SEVER_LINE_FRAMING:
        return (message + "\n").encode("ascii")
    raise ValueError("unknown TCP framing mode")


def _atomic_json(path: Path, payload: object) -> None:
    atomic_json_write(path, payload)


@dataclass(frozen=True, slots=True)
class HUNLTCPClientTelemetry:
    schema: str
    name: str
    blueprint_sha256: str
    framing: str
    action_delay_sec: float
    hands_started: int
    settlements_received: int
    decisions: int
    trained_exact_decisions: int
    trained_backoff_decisions: int
    trained_derived_policy_decisions: int
    trained_nonuniform_policy_decisions: int
    max_trained_policy_l1_from_uniform: float
    uniform_emergency_decisions: int
    lookup_source_counts: dict[str, int]
    sends: int
    bytes_received: int
    bytes_sent: int
    elapsed_sec: float
    max_decision_compute_ms: float
    clean_connection_close: bool
    local_sever_complete_70_hands: bool
    official_natural_70_boundary: bool
    external_thp_state_69_required: bool
    official_wire_alone_proves_complete: bool
    certification_claimed: bool
    complete_70_hands: bool


def _is_local_sever_complete(runtime: CommonA2StrategyRuntime) -> bool:
    state = runtime.session.current
    if (
        runtime.session.hands_started != 70
        or runtime.session.settlements_received != 70
        or state is None
        or not state.is_terminal
        or runtime.session.pending_decision_id is not None
    ):
        return False
    return state.terminal_reason != "showdown" or runtime.session.current_showdown


def _connection_close_status(
    runtime: CommonA2StrategyRuntime,
    *,
    framing: str,
    saw_eof: bool,
) -> dict[str, bool]:
    """Separate local completeness from the official 69+THP boundary."""

    if runtime.decoder.buffered:
        raise RuntimeError("connection closed with undecoded protocol bytes")
    local_complete = _is_local_sever_complete(runtime)
    if framing == SEVER_LINE_FRAMING:
        if not local_complete:
            raise RuntimeError(
                "local sever connection ended before Common proved 70 settlements"
            )
        return {
            "certification_claimed": False,
            "clean_connection_close": True,
            "external_thp_state_69_required": False,
            "local_sever_complete_70_hands": True,
            "official_natural_70_boundary": False,
            "official_wire_alone_proves_complete": False,
        }
    if framing != OFFICIAL_RAW_FRAMING:
        raise ValueError("unknown TCP framing mode")
    if not saw_eof:
        raise RuntimeError("official raw connection has no EOF completion boundary")
    evidence = runtime.session.connection_close_evidence()
    if not evidence["natural_70_boundary"]:
        raise RuntimeError(
            "official raw connection closed before Common proved the natural hand-70 boundary"
        )
    # The official 2021 EXE's natural 69-settlement shape still needs the
    # external THP state 69/footer.  This client can report a clean boundary,
    # but never upgrades wire evidence into certification.
    return {
        "certification_claimed": False,
        "clean_connection_close": True,
        "external_thp_state_69_required": bool(evidence["requires_thp_state_69"]),
        "local_sever_complete_70_hands": False,
        "official_natural_70_boundary": True,
        "official_wire_alone_proves_complete": bool(
            evidence["wire_alone_proves_complete"]
        ),
    }


def run_hunl_tcp_client(
    host: str,
    port: int,
    *,
    name: str,
    blueprint: HUNLBlueprint,
    seed: int,
    action_delay_sec: float = OFFICIAL_ACTION_DELAY_SEC,
    framing: str = OFFICIAL_RAW_FRAMING,
    connect_timeout_sec: float = 30.0,
    match_timeout_sec: float = 180.0,
    read_quiet_sec: float = 0.005,
    stop_event: threading.Event | None = None,
) -> HUNLTCPClientTelemetry:
    """Run one persistent 70-hand connection on the socket-owner thread."""

    if type(host) is not str or not host:
        raise ValueError("host must be non-empty")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be an integer in 1..65535")
    if type(blueprint) is not HUNLBlueprint:
        raise TypeError("blueprint must be the exact HUNLBlueprint type")
    if framing not in (OFFICIAL_RAW_FRAMING, SEVER_LINE_FRAMING):
        raise ValueError("unknown TCP framing mode")
    if type(action_delay_sec) not in (int, float) or not 0.0 <= float(action_delay_sec) <= 5.0:
        raise ValueError("action delay must lie in [0, 5]")
    if type(match_timeout_sec) not in (int, float) or match_timeout_sec <= 0:
        raise ValueError("match timeout must be positive")
    if type(read_quiet_sec) not in (int, float) or read_quiet_sec <= 0:
        raise ValueError("read quiet interval must be positive")
    if stop_event is not None and not isinstance(stop_event, threading.Event):
        raise TypeError("stop_event must be a threading.Event or None")
    if stop_event is not None and stop_event.is_set():
        raise RuntimeError("HUNL TCP client stopped before connection")

    runtime = CommonA2StrategyRuntime(name, blueprint, seed=seed)
    started = time.perf_counter()
    deadline = started + float(match_timeout_sec)
    sends = 0
    received = 0
    sent = 0
    last_platform_message_at = started
    saw_eof = False

    def transmit(sock: socket.socket, event_kind: str, message: str) -> None:
        nonlocal sends, sent
        if event_kind != "name_requested":
            remaining = float(action_delay_sec) - (
                time.perf_counter() - last_platform_message_at
            )
            if remaining > 0.0:
                time.sleep(remaining)
        payload = encode_client_message(message, framing)
        sock.sendall(payload)
        sends += 1
        sent += len(payload)

    with socket.create_connection(
        (host, port), timeout=float(connect_timeout_sec)
    ) as sock:
        sock.settimeout(float(read_quiet_sec))
        while time.perf_counter() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("HUNL TCP client stopped by its socket owner")
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                outputs = runtime.flush_numeric()
                for event, outgoing in outputs:
                    if outgoing is not None:
                        transmit(sock, event.kind, outgoing)
                if framing == SEVER_LINE_FRAMING and _is_local_sever_complete(runtime):
                    break
                continue
            if not chunk:
                saw_eof = True
                for token in runtime.decoder.finish():
                    event, outgoing = runtime.on_token(token)
                    if outgoing is not None:
                        raise RuntimeError(
                            "platform closed while a name/action response was still owed"
                        )
                break
            received += len(chunk)
            last_platform_message_at = time.perf_counter()
            for event, outgoing in runtime.feed(chunk):
                if outgoing is not None:
                    transmit(sock, event.kind, outgoing)
            if framing == SEVER_LINE_FRAMING and _is_local_sever_complete(runtime):
                break
        else:
            raise TimeoutError("HUNL TCP client exceeded its complete-match deadline")

    close_status = _connection_close_status(
        runtime,
        framing=framing,
        saw_eof=saw_eof,
    )
    elapsed = time.perf_counter() - started
    compute_ms = [item.decision_compute_ns / 1_000_000 for item in runtime.trace]
    lookup_source_counts: dict[str, int] = {}
    for item in runtime.trace:
        lookup_source_counts[item.lookup_source] = (
            lookup_source_counts.get(item.lookup_source, 0) + 1
        )
    trained_exact = lookup_source_counts.get("trained_exact_row", 0)
    trained_backoff = sum(
        count
        for source, count in lookup_source_counts.items()
        if source.startswith("trained_backoff_")
    )
    uniform_emergency = lookup_source_counts.get(HUNL_FALLBACK_CONTRACT["mode"], 0)
    if trained_exact + trained_backoff + uniform_emergency != runtime.decisions_completed:
        raise AssertionError("HUNL lookup-source telemetry is not exhaustive")
    trained_nonuniform = sum(
        item.lookup_source != HUNL_FALLBACK_CONTRACT["mode"]
        and item.policy_l1_from_uniform > HUNL_MATERIAL_POLICY_L1_THRESHOLD
        for item in runtime.trace
    )
    max_trained_l1 = max(
        (
            item.policy_l1_from_uniform
            for item in runtime.trace
            if item.lookup_source != HUNL_FALLBACK_CONTRACT["mode"]
        ),
        default=0.0,
    )
    return HUNLTCPClientTelemetry(
        schema=TCP_CLIENT_SCHEMA,
        name=name,
        blueprint_sha256=blueprint.digest,
        framing=framing,
        action_delay_sec=float(action_delay_sec),
        hands_started=runtime.session.hands_started,
        settlements_received=runtime.session.settlements_received,
        decisions=runtime.decisions_completed,
        trained_exact_decisions=trained_exact,
        trained_backoff_decisions=trained_backoff,
        trained_derived_policy_decisions=trained_exact + trained_backoff,
        trained_nonuniform_policy_decisions=trained_nonuniform,
        max_trained_policy_l1_from_uniform=max_trained_l1,
        uniform_emergency_decisions=uniform_emergency,
        lookup_source_counts=dict(sorted(lookup_source_counts.items())),
        sends=sends,
        bytes_received=received,
        bytes_sent=sent,
        elapsed_sec=elapsed,
        max_decision_compute_ms=max(compute_ms, default=0.0),
        clean_connection_close=close_status["clean_connection_close"],
        local_sever_complete_70_hands=close_status[
            "local_sever_complete_70_hands"
        ],
        official_natural_70_boundary=close_status[
            "official_natural_70_boundary"
        ],
        external_thp_state_69_required=close_status[
            "external_thp_state_69_required"
        ],
        official_wire_alone_proves_complete=close_status[
            "official_wire_alone_proves_complete"
        ],
        certification_claimed=close_status["certification_claimed"],
        complete_70_hands=close_status["local_sever_complete_70_hands"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", required=True)
    parser.add_argument("--blueprint", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--action-delay",
        type=float,
        default=OFFICIAL_ACTION_DELAY_SEC,
        help="official-safe default is 0.30 seconds",
    )
    parser.add_argument(
        "--sever-line",
        action="store_true",
        help="explicit local adapter for sever/server/tcp_server.py line reads",
    )
    parser.add_argument("--telemetry", type=Path)
    args = parser.parse_args()
    result = run_hunl_tcp_client(
        args.host,
        args.port,
        name=args.name,
        blueprint=HUNLBlueprint.load(args.blueprint),
        seed=args.seed,
        action_delay_sec=args.action_delay,
        framing=SEVER_LINE_FRAMING if args.sever_line else OFFICIAL_RAW_FRAMING,
    )
    payload = asdict(result)
    if args.telemetry is not None:
        _atomic_json(args.telemetry, payload)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
