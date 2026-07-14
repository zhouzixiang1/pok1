from __future__ import annotations

import hashlib
import json

import pytest

from bots.research_native_lab.common_contracts.deal_generator import (
    build_70_hand_commitment,
)
from bots.research_native_lab.common_contracts.native_harness import (
    run_development_tcp_capture_sync,
)
from bots.research_native_lab.common_contracts.native_wire import (
    WIRE_CAPTURE_FRAMING,
    WIRE_CAPTURE_SCHEMA,
)


def test_real_socket_harness_captures_exact_70_decks_wire_and_monotonic_timing() -> None:
    commitment = build_70_hand_commitment(
        int.from_bytes(hashlib.sha256(b"native-harness-e2e-root").digest(), "big")
    )
    result = run_development_tcp_capture_sync(commitment, decision_budget_ms=250)
    verified = result.verified_replay

    assert verified.clean_complete
    assert verified.hands_started == verified.hands_played == 70
    assert verified.actual_dealt_prefix_digests == commitment.deck_digests
    assert verified.raw_wire_digest == hashlib.sha256(result.raw_wire).hexdigest()
    assert verified.decision_budget_ms == 250
    assert verified.execution_binding_authority == "development_diagnostic_only"
    assert tuple(map(len, verified.decision_wait_ns_by_connection)) == (35, 35)
    assert verified.search_nodes_by_connection == (0, 0)
    assert verified.fallback_decisions_by_connection == (0, 0)
    assert verified.telemetry_complete_by_connection == (False, False)
    assert verified.wire_semantics_verified is True
    assert len(bytes.fromhex(verified.wire_semantic_binding_digest)) == 32
    assert all(
        wait <= 250_000_000
        for connection in verified.decision_wait_ns_by_connection
        for wait in connection
    )

    wire = json.loads(result.raw_wire)
    assert wire["schema"] == WIRE_CAPTURE_SCHEMA
    assert wire["framing"] == WIRE_CAPTURE_FRAMING
    records = wire["records"]
    assert records
    assert [row["sequence"] for row in records] == list(range(len(records)))
    assert {row["connection_index"] for row in records} == {0, 1}
    assert {row["direction"] for row in records} == {
        "server_to_bot",
        "bot_to_server",
    }
    assert all(
        records[index]["monotonic_ns"] <= records[index + 1]["monotonic_ns"]
        for index in range(len(records) - 1)
    )
    tokens = wire["tokens"]
    assert len(tokens) == 72
    assert [row["sequence"] for row in tokens] == list(range(len(tokens)))
    assert sum(row["message_type"] == "name" for row in tokens) == 2
    assert sum(row["message_type"] == "action" for row in tokens) == 70


def test_server_only_reference_harness_reports_local_strength_zero_send_delay() -> None:
    # Positive official delay requires a trusted client-side send trace; the
    # reference E2E deliberately exposes only local-strength delay=0.
    commitment = build_70_hand_commitment(
        int.from_bytes(hashlib.sha256(b"native-harness-delay-root").digest(), "big")
    )
    result = run_development_tcp_capture_sync(commitment, decision_budget_ms=5_000)
    assert result.verified_replay.action_send_delay_ms == 0


@pytest.mark.parametrize(
    ("scenario", "expected_decisions"),
    (
        ("checkdown", (280, 280)),
        ("allin", (70, 70)),
        ("minraise_split", (280, 280)),
    ),
)
def test_real_socket_showdown_raise_split_and_allin_runout_are_replay_verified(
    scenario: str,
    expected_decisions: tuple[int, int],
) -> None:
    commitment = build_70_hand_commitment(
        int.from_bytes(
            hashlib.sha256(f"native-harness-{scenario}".encode("ascii")).digest(),
            "big",
        )
    )
    result = run_development_tcp_capture_sync(
        commitment,
        decision_budget_ms=250,
        scenario=scenario,
    )
    verified = result.verified_replay
    assert verified.clean_complete
    assert verified.wire_semantics_verified
    assert tuple(map(len, verified.decision_wait_ns_by_connection)) == (
        expected_decisions
    )
    replay = json.loads(result.raw_replay)
    settlements = [
        event for event in replay["events"] if event.get("type") == "settle"
    ]
    assert len(settlements) == 70
    assert all(event["is_showdown"] is True for event in settlements)
