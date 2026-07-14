from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bots.research_native_lab.common_contracts.deal_generator import (
    build_70_hand_commitment,
    generate_tcp_deck,
    tcp_card_from_id,
    tcp_card_to_wire,
)
from bots.research_native_lab.common_contracts.cards import (
    compare_hands,
    tcp_card_to_int,
)
from bots.research_native_lab.common_contracts.native_replay import (
    bind_authorized_supervisor_replay,
    MAX_NATIVE_REPLAY_BYTES,
    NATIVE_REPLAY_VERIFIER_DIGEST,
    PartialFaultKind,
    PartialReplayFault,
    ReplayExecutionBinding,
    ReplayVerificationError,
    VerifiedNativeReplay,
    verify_native_replay,
)
from bots.research_native_lab.common_contracts.native_harness import (
    run_development_tcp_capture_sync,
)
from bots.research_native_lab.common_contracts.native_wire import (
    verify_structured_wire_capture,
)
from bots.research_native_lab.common_contracts.evaluation import (
    LegPlan,
    MatchObservation,
    ReplayVerificationReceipt,
)
from bots.research_native_lab.common_contracts.resource_enforcer import (
    DecisionEnforcementEvent,
    FormalEnforcementUnavailable,
)
from bots.research_native_lab.common_contracts.tests.test_native_wire import (
    _decision_enforcement_events,
)
from bots.research_native_lab.common_contracts.tests.test_resource_enforcer import (
    _authorized_supervisor_leg_fixture,
    _profile,
)


ROOT = int.from_bytes(bytes(range(32)), "big")
COMMITMENT = build_70_hand_commitment(ROOT)
RAW_WIRE = b"native-wire-fixture-v1\x00connection-0\x00connection-1"


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _development_binding(
    *,
    leg_plan_digest: str | None = None,
    connection_identity_digests: tuple[str, str] | None = None,
    run_ids_by_connection: tuple[str, str] | None = None,
    process_tree_ids_by_connection: tuple[str, str] | None = None,
    cgroup_paths_by_connection: tuple[str, str] | None = None,
    resource_profile_digest: str | None = None,
    decision_budget_ms: int = 5_000,
    platform_action_timeout_ms: int = 60_000,
    action_send_delay_ms: int = 0,
    raw_wire: bytes = RAW_WIRE,
) -> ReplayExecutionBinding:
    return ReplayExecutionBinding.for_development(
        leg_plan_digest=leg_plan_digest or _h("native-replay-leg"),
        connection_identity_digests=(
            connection_identity_digests
            or (_h("native-replay-identity-0"), _h("native-replay-identity-1"))
        ),
        run_ids_by_connection=(
            run_ids_by_connection
            or (_h("native-replay-run-0"), _h("native-replay-run-1"))
        ),
        process_tree_ids_by_connection=(
            process_tree_ids_by_connection
            or ("native-replay-process-0", "native-replay-process-1")
        ),
        cgroup_paths_by_connection=(
            cgroup_paths_by_connection
            or (
                "/sys/fs/cgroup/pok/native-replay/0",
                "/sys/fs/cgroup/pok/native-replay/1",
            )
        ),
        resource_profile_digest=(
            resource_profile_digest or _h("native-replay-resource-profile")
        ),
        decision_budget_ms=decision_budget_ms,
        platform_action_timeout_ms=platform_action_timeout_ms,
        action_send_delay_ms=action_send_delay_ms,
        raw_wire=raw_wire,
    )


BINDING = _development_binding()


def _json_bytes(payload) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _wire_cards(cards) -> list[str]:
    return [tcp_card_to_wire(card) for card in cards]


def _local_cards(cards) -> tuple[int, ...]:
    return tuple(tcp_card_to_int(*tcp_card_from_id(card)) for card in cards)


def _request_and_action(
    events: list[dict],
    *,
    hand: int,
    player: int,
    stage: str,
    action: str,
    pot_before: int,
    player_bets_before: list[int],
    player_chips_after: list[int],
    amount: int | None = None,
    needed: int | None = None,
    pot_after: int | None = None,
    reason: str | None = None,
    binding: ReplayExecutionBinding = BINDING,
) -> None:
    decision_index = sum(
        1
        for event in events
        if event.get("type") == "action" and event.get("player_idx") == player
    )
    requested_epoch_ms = 1_800_000_000_000 + len(events) * 100_000
    requested_monotonic_ns = 10_000_000_000 + len(events) * 100_000_000_000
    events.append(
        {
            "type": "action_requested",
            "hand": hand,
            "player_idx": player,
            "stage": stage,
            "pot": pot_before,
            "player_bets": player_bets_before,
            "timeout_budget_sec": 60.0,
            "decision_budget_ms": binding.decision_budget_ms,
            "platform_action_timeout_ms": binding.platform_action_timeout_ms,
            "action_send_delay_ms": binding.action_send_delay_ms,
            "requested_epoch_ms": requested_epoch_ms,
            "compute_deadline_epoch_ms": (
                requested_epoch_ms + binding.decision_budget_ms
            ),
            "deadline_epoch_ms": (
                requested_epoch_ms + binding.platform_action_timeout_ms
            ),
            "requested_monotonic_ns": requested_monotonic_ns,
            "compute_deadline_monotonic_ns": (
                requested_monotonic_ns + binding.decision_budget_ms * 1_000_000
            ),
            "platform_deadline_monotonic_ns": (
                requested_monotonic_ns
                + binding.platform_action_timeout_ms * 1_000_000
            ),
        }
    )
    is_timeout = action == "timeout"
    action_epoch_ms = (
        requested_epoch_ms + binding.platform_action_timeout_ms
        if is_timeout
        else requested_epoch_ms + 1 + binding.action_send_delay_ms
    )
    compute_finished_monotonic_ns = requested_monotonic_ns + 1_000_000
    action_monotonic_ns = (
        requested_monotonic_ns
        + binding.platform_action_timeout_ms * 1_000_000
        if is_timeout
        else compute_finished_monotonic_ns
        + binding.action_send_delay_ms * 1_000_000
    )
    wait_ms = action_epoch_ms - requested_epoch_ms
    wait_ns = action_monotonic_ns - requested_monotonic_ns
    event = {
        "type": "action",
        "hand": hand,
        "player_idx": player,
        "stage": stage,
        "action": action,
        "decision_index": decision_index,
        "search_nodes": 0 if is_timeout else 100,
        "fallback_used": False,
        "snapshot_tier": "timeout-no-snapshot" if is_timeout else "first-search",
        "telemetry_source": "trusted_worker_trace",
        "decision_wait_sec": wait_ms / 1000,
        "decision_wait_ms": wait_ms,
        "decision_wait_ns": wait_ns,
        "timeout_budget_sec": 60.0,
        "decision_budget_ms": binding.decision_budget_ms,
        "platform_action_timeout_ms": binding.platform_action_timeout_ms,
        "action_send_delay_ms": binding.action_send_delay_ms,
        "action_epoch_ms": action_epoch_ms,
        "action_monotonic_ns": action_monotonic_ns,
        "player_chips": player_chips_after,
    }
    if not is_timeout:
        event["compute_finished_monotonic_ns"] = compute_finished_monotonic_ns
        event["send_not_before_monotonic_ns"] = (
            compute_finished_monotonic_ns
            + binding.action_send_delay_ms * 1_000_000
        )
    if amount is not None:
        event["amount"] = amount
    if needed is not None:
        event["needed"] = needed
    if pot_after is not None:
        event["pot"] = pot_after
    if reason is not None:
        event["reason"] = reason
    events.append(event)


def _captured_payload(
    completed_hands: int = 70,
    *,
    timeout_on_last: bool = False,
    binding: ReplayExecutionBinding = BINDING,
) -> dict:
    events: list[dict] = [
        {
            "type": "client_order",
            "order": ["BotA", "BotB"],
            "connection_order": ["BotA", "BotB"],
            "connection_identity_digests": list(
                binding.connection_identity_digests
            ),
            "run_ids_by_connection": list(binding.run_ids_by_connection),
            "process_tree_ids_by_connection": list(
                binding.process_tree_ids_by_connection
            ),
            "cgroup_paths_by_connection": list(
                binding.cgroup_paths_by_connection
            ),
            "connection_binding_digests": list(
                binding.connection_binding_digests()
            ),
        }
    ]
    settlements: list[dict] = []
    totals = [0, 0]
    timeout_counts = [0, 0]

    for hand in range(1, completed_hands + 1):
        deck = generate_tcp_deck(COMMITMENT.hand_seeds[hand - 1])
        sb_idx = (hand - 1) % 2
        bb_idx = 1 - sb_idx
        chips = [20_000, 20_000]
        chips[sb_idx] -= 50
        chips[bb_idx] -= 100
        holes: list[list[str] | None] = [None, None]
        holes[sb_idx] = _wire_cards(deck[0:2])
        holes[bb_idx] = _wire_cards(deck[2:4])
        events.append(
            {
                "type": "hand_start",
                "hand": hand,
                "sb_idx": sb_idx,
                "bb_idx": bb_idx,
                "names": ["BotA", "BotB"],
                "player_chips": chips,
                "pot": 150,
            }
        )
        events.append(
            {
                "type": "cards_dealt",
                "hand": hand,
                "hole_cards": holes,
            }
        )

        if hand == 1:
            _request_and_action(
                events,
                hand=hand,
                player=sb_idx,
                stage="preflop",
                action="call",
                pot_before=150,
                player_bets_before=[50, 100],
                player_chips_after=[19_900, 19_900],
                amount=50,
                pot_after=200,
                binding=binding,
            )
            _request_and_action(
                events,
                hand=hand,
                player=bb_idx,
                stage="preflop",
                action="check",
                pot_before=200,
                player_bets_before=[100, 100],
                player_chips_after=[19_900, 19_900],
                binding=binding,
            )
            for stage, start, end in (
                ("flop", 4, 7),
                ("turn", 7, 8),
                ("river", 8, 9),
            ):
                events.append(
                    {
                        "type": "stage",
                        "hand": hand,
                        "stage": stage,
                        "cards": _wire_cards(deck[start:end]),
                        "player_chips": [19_900, 19_900],
                    }
                )
                _request_and_action(
                    events,
                    hand=hand,
                    player=bb_idx,
                    stage=stage,
                    action="check",
                    pot_before=200,
                    player_bets_before=[0, 0],
                    player_chips_after=[19_900, 19_900],
                    binding=binding,
                )
                _request_and_action(
                    events,
                    hand=hand,
                    player=sb_idx,
                    stage=stage,
                    action="call",
                    pot_before=200,
                    player_bets_before=[0, 0],
                    player_chips_after=[19_900, 19_900],
                    amount=0,
                    pot_after=200,
                    binding=binding,
                )
            comparison = compare_hands(
                _local_cards((*deck[0:2], *deck[4:9])),
                _local_cards((*deck[2:4], *deck[4:9])),
            )
            earnings = [0, 0]
            winner_idx = None
            if comparison > 0:
                earnings[sb_idx] = 100
                earnings[bb_idx] = -100
                winner_idx = sb_idx
            elif comparison < 0:
                earnings[sb_idx] = -100
                earnings[bb_idx] = 100
                winner_idx = bb_idx
            event_settlement = {
                "type": "settle",
                "hand": hand,
                "is_showdown": True,
                "winner_idx": winner_idx,
                "pot": 200,
                "earnings": earnings,
                "sb_idx": sb_idx,
                "bb_idx": bb_idx,
                "sb_cards": _wire_cards(deck[0:2]),
                "bb_cards": _wire_cards(deck[2:4]),
                "community": _wire_cards(deck[4:9]),
                "sb_hand": "pair",
                "bb_hand": "pair",
                "player_chips": [19_900, 19_900],
            }
        else:
            action = (
                "timeout"
                if timeout_on_last and hand == completed_hands
                else "fold"
            )
            _request_and_action(
                events,
                hand=hand,
                player=sb_idx,
                stage="preflop",
                action=action,
                pot_before=150,
                player_bets_before=(
                    [50, 100] if sb_idx == 0 else [100, 50]
                ),
                player_chips_after=chips,
                binding=binding,
            )
            if action == "timeout":
                timeout_counts[sb_idx] += 1
            earnings = [0, 0]
            earnings[sb_idx] = -50
            earnings[bb_idx] = 50
            event_settlement = {
                "type": "settle",
                "hand": hand,
                "is_showdown": False,
                "winner_idx": bb_idx,
                "pot": 150,
                "earnings": earnings,
                "reason": f"Bot{chr(ord('A') + sb_idx)} folded",
                "player_chips": chips,
            }

        events.append(event_settlement)
        summary = {
            key: event_settlement.get(key, "" if key == "reason" else None)
            for key in (
                "hand",
                "earnings",
                "pot",
                "is_showdown",
                "winner_idx",
                "reason",
            )
        }
        settlements.append(summary)
        totals[0] += event_settlement["earnings"][0]
        totals[1] += event_settlement["earnings"][1]

    match_end = {
        "type": "match_end",
        "total_earnings": totals,
        "names": ["BotA", "BotB"],
        "hands_played": completed_hands,
    }
    if completed_hands == 70:
        match_end["result_finalized_epoch_ms"] = max(
            event.get("action_epoch_ms", 0) for event in events
        ) + 1
    events.append(match_end)
    player_issues = [[], []]
    for player_idx, count in enumerate(timeout_counts):
        if count:
            label = ("BotA", "BotB")[player_idx]
            player_issues[player_idx].append(f"{label}: timeouts={count}")
    issues = [issue for rows in player_issues for issue in rows]
    return {
        "execution_binding": binding.capture_payload(),
        "bot_a": "BotA",
        "bot_b": "BotB",
        "hands_requested": 70,
        "hands_played": completed_hands,
        "execution_mode": "native_tcp",
        "wrapper_used": False,
        "wrapper_used_by_player": {"BotA": False, "BotB": False},
        "per_player": {
            "BotA": {
                "earnings": totals[0],
                "illegal_actions": 0,
                "timeouts": timeout_counts[0],
                "wrapper_used": False,
                "passed_compliance": not player_issues[0],
                "compliance_issues": player_issues[0],
                "native": {
                    "returncode": 0,
                    "process_failures": 0,
                    "json_response_stdout": 0,
                },
            },
            "BotB": {
                "earnings": totals[1],
                "illegal_actions": 0,
                "timeouts": timeout_counts[1],
                "wrapper_used": False,
                "passed_compliance": not player_issues[1],
                "compliance_issues": player_issues[1],
                "native": {
                    "returncode": 0,
                    "process_failures": 0,
                    "json_response_stdout": 0,
                },
            },
        },
        "net_chips_a": totals[0],
        "net_chips_b": totals[1],
        "settlements": settlements,
        "passed_compliance": not issues,
        "issues": issues,
        "events_tail": events[-20:],
        "events": events,
    }


def _verify_payload(
    payload: dict,
    *,
    fault=None,
    binding: ReplayExecutionBinding = BINDING,
    raw_wire: bytes = RAW_WIRE,
) -> VerifiedNativeReplay:
    return verify_native_replay(
        _json_bytes(payload),
        COMMITMENT,
        execution_binding=binding,
        raw_wire=raw_wire,
        partial_fault=fault,
    )


def _replace_last_timeout_with_tokenless_fault(
    payload: dict,
    *,
    action: str,
    kind: PartialFaultKind,
) -> PartialReplayFault:
    event = next(
        item
        for item in payload["events"]
        if item.get("hand") == 3 and item.get("action") == "timeout"
    )
    event["action"] = action
    event["snapshot_tier"] = "fault-no-action"
    event["search_nodes"] = 0
    request = next(
        item
        for item in payload["events"]
        if item.get("hand") == 3 and item.get("type") == "action_requested"
    )
    event["action_epoch_ms"] = request["requested_epoch_ms"] + 1
    event["action_monotonic_ns"] = request["requested_monotonic_ns"] + 1_000_000
    event["decision_wait_sec"] = 0.001
    event["decision_wait_ms"] = 1
    event["decision_wait_ns"] = 1_000_000
    payload["per_player"]["BotA"]["timeouts"] = 0
    payload["per_player"]["BotA"]["compliance_issues"] = []
    owner: int | None = 0
    if kind is PartialFaultKind.INFRASTRUCTURE:
        owner = None
        payload["per_player"]["BotA"]["passed_compliance"] = True
        issue = "infrastructure: supervised match result unavailable"
    else:
        issue = f"BotA: {kind.value}"
        payload["per_player"]["BotA"]["passed_compliance"] = False
        payload["per_player"]["BotA"]["compliance_issues"] = [issue]
        if kind is PartialFaultKind.CRASH:
            payload["per_player"]["BotA"]["native"].update(
                returncode=1,
                process_failures=1,
            )
    payload["passed_compliance"] = False
    payload["issues"] = [issue]
    payload["events_tail"] = payload["events"][-20:]
    return PartialReplayFault(
        kind=kind,
        owner_connection=owner,
        hand_number=3,
        evidence_digest=_h(f"tokenless-{kind.value}-evidence"),
    )


def test_complete_replay_golden_vector_and_token_fields() -> None:
    payload = _captured_payload()
    raw = _json_bytes(payload)
    verified = verify_native_replay(
        raw,
        COMMITMENT,
        execution_binding=BINDING,
        raw_wire=RAW_WIRE,
    )

    assert verified.clean_complete
    assert verified.hands_started == 70
    assert verified.hands_played == 70
    assert verified.settlement_count == 70
    assert verified.net_chips_by_connection == (150, -150)
    assert verified.timeout_count_by_connection == (0, 0)
    assert verified.illegal_action_count_by_connection == (0, 0)
    assert len(verified.full_deck_digests) == 70
    assert verified.full_deck_digests == COMMITMENT.deck_digests
    assert len(verified.event_digests) == len(payload["events"])
    assert len(set(verified.event_digests)) == len(verified.event_digests)
    assert verified.hand70_evidence_digest is not None
    assert verified.raw_replay_digest == hashlib.sha256(raw).hexdigest()
    assert verified.canonical_replay_digest == verified.raw_replay_digest
    assert verified.execution_binding_digest == BINDING.binding_digest
    assert verified.leg_plan_digest == BINDING.leg_plan_digest
    assert verified.connection_identity_digests == BINDING.connection_identity_digests
    assert verified.run_ids_by_connection == BINDING.run_ids_by_connection
    assert verified.raw_wire_digest == hashlib.sha256(RAW_WIRE).hexdigest()
    assert verified.wire_semantics_verified is False
    assert verified.wire_semantic_binding_digest == (
        "658f5778fdcfbde07f061ef86300efddfa4ad778b610fda6f0e3210e3b047ef7"
    )
    assert verified.result_finalized_epoch_ms == 1_800_036_500_002
    assert tuple(map(len, verified.decision_wait_ns_by_connection)) == (38, 39)
    assert verified.search_nodes_by_connection == (3_800, 3_900)
    assert verified.fallback_decisions_by_connection == (0, 0)
    assert verified._assert_verified() is None

    # Fixed values catch drift in canonicalization, event ordering, card
    # mapping and the hand-70 evidence construction.
    assert NATIVE_REPLAY_VERIFIER_DIGEST == (
        "a11ce011bb2319ee8baa004ac2bce5cc9f3a1b15dbda820dbd598fbd2d793763"
    )
    assert verified.raw_digest == (
        "b22dca502355baa57d71d81cdc8e1e1e755ce5d7811a3440b00ce21813c1cd58"
    )
    assert verified.event_digests[0] == (
        "927d39d24bc5dd2861e5f9ff9064403f4f6f6b01640b5d1b0a90c75b139aa19e"
    )
    assert verified.event_digests[-1] == (
        "3e4802e210156f1a88c184fd56df7520539f3eae7e7d0ed32b9be273ec863e09"
    )
    assert verified.hand70_evidence_digest == (
        "7a6bb21b40a0cc05e2605ce166872cf64f6adf872e07d8b7480848c98428985c"
    )
    assert verified.verification_evidence_digest == (
        "39618df9622400f9f550087247f9e6c11cfc4eb148f7eefab38da4447414b208"
    )


def test_verified_token_cannot_be_directly_constructed() -> None:
    verified = _verify_payload(_captured_payload())
    with pytest.raises(TypeError, match="issued only"):
        VerifiedNativeReplay(
            _token=object(),
            execution_binding=BINDING,
            raw_digest=verified.raw_digest,
            canonical_digest=verified.canonical_digest,
            verification_evidence_digest=verified.verification_evidence_digest,
            full_deck_digests=verified.full_deck_digests,
            event_digests=verified.event_digests,
            hands_started=verified.hands_started,
            hands_played=verified.hands_played,
            settlement_count=verified.settlement_count,
            net_chips_by_connection=verified.net_chips_by_connection,
            timeout_count_by_connection=verified.timeout_count_by_connection,
            illegal_action_count_by_connection=verified.illegal_action_count_by_connection,
            decision_wait_ns_by_connection=verified.decision_wait_ns_by_connection,
            search_nodes_by_connection=verified.search_nodes_by_connection,
            fallback_decisions_by_connection=(
                verified.fallback_decisions_by_connection
            ),
            decision_trace_digest_by_connection=(
                verified.decision_trace_digest_by_connection
            ),
            telemetry_complete_by_connection=(
                verified.telemetry_complete_by_connection
            ),
            wire_semantics_verified=verified.wire_semantics_verified,
            wire_semantic_binding_digest=(
                verified.wire_semantic_binding_digest
            ),
            hand70_evidence_digest=verified.hand70_evidence_digest,
            result_finalized_epoch_ms=verified.result_finalized_epoch_ms,
            partial_fault=None,
        )


def test_execution_resource_receipts_alone_cannot_mint_formal_replay_binding() -> None:
    with pytest.raises(ValueError, match="AuthorizedSupervisorLeg"):
        ReplayExecutionBinding.from_formal_execution_receipts(
            leg_plan=object(),
            execution_receipts=(),
            resource_receipts=(),
            resource_profile=object(),
            raw_wire=RAW_WIRE,
        )


def test_development_replay_carries_no_supervisor_authority() -> None:
    verified = _verify_payload(_captured_payload())
    assert verified.execution_binding_authority == "development_diagnostic_only"
    assert verified.supervisor_contract_digest is None
    assert verified.supervisor_leg_receipt_digest is None
    assert verified.supervisor_receipt_consumption_key is None
    assert verified.supervisor_capture_session_digest is None


def test_verified_capability_rejects_replace_copy_and_field_mutation() -> None:
    verified = _verify_payload(_captured_payload())

    with pytest.raises(TypeError):
        dataclasses.replace(verified, hands_played=69)

    copied = copy.copy(verified)
    with pytest.raises(TypeError, match="copied or forged"):
        copied._assert_verified()

    field_tampered = _verify_payload(_captured_payload())
    object.__setattr__(field_tampered, "hands_played", 69)
    with pytest.raises(TypeError, match="evidence was altered"):
        field_tampered._assert_verified()


def test_explicit_partial_timeout_is_bound_but_not_finalized() -> None:
    payload = _captured_payload(3, timeout_on_last=True)
    fault = PartialReplayFault(
        kind=PartialFaultKind.TIMEOUT,
        owner_connection=0,
        hand_number=3,
        evidence_digest=hashlib.sha256(b"timeout-evidence").hexdigest(),
    )
    verified = _verify_payload(payload, fault=fault)

    assert not verified.clean_complete
    assert verified.hands_started == 3
    assert verified.hands_played == 3
    assert verified.timeout_count_by_connection == (1, 0)
    assert verified.partial_fault == fault
    assert verified.hand70_evidence_digest is None

    with pytest.raises(ReplayVerificationError, match="clean replay"):
        _verify_payload(payload)


def test_complete_result_cannot_receive_a_post_hoc_fault() -> None:
    fault = PartialReplayFault(
        kind=PartialFaultKind.CRASH,
        owner_connection=0,
        hand_number=70,
        evidence_digest=hashlib.sha256(b"fabricated-after-result").hexdigest(),
    )
    with pytest.raises(ReplayVerificationError, match="post-hoc fault"):
        _verify_payload(_captured_payload(), fault=fault)


def test_partial_action_fault_must_exist_in_the_final_hand_events() -> None:
    payload = _captured_payload(3, timeout_on_last=True)
    wrong_owner = PartialReplayFault(
        kind=PartialFaultKind.TIMEOUT,
        owner_connection=1,
        hand_number=3,
        evidence_digest=hashlib.sha256(b"wrong-owner").hexdigest(),
    )
    with pytest.raises(ReplayVerificationError, match="differs from captured"):
        _verify_payload(payload, fault=wrong_owner)


@pytest.mark.parametrize(
    ("action", "kind"),
    (
        ("fault:crash", PartialFaultKind.CRASH),
        ("fault:resource_overrun", PartialFaultKind.RESOURCE_OVERRUN),
        ("fault:protocol", PartialFaultKind.PROTOCOL),
        ("fault:infrastructure", PartialFaultKind.INFRASTRUCTURE),
    ),
)
def test_tokenless_non_timeout_fault_is_exactly_bound_to_partial_adjudication(
    action: str,
    kind: PartialFaultKind,
) -> None:
    payload = _captured_payload(3, timeout_on_last=True)
    fault = _replace_last_timeout_with_tokenless_fault(
        payload,
        action=action,
        kind=kind,
    )
    verified = _verify_payload(payload, fault=fault)
    assert verified.partial_fault == fault
    assert verified.timeout_count_by_connection == (0, 0)

    replacement_kind = (
        PartialFaultKind.PROTOCOL
        if kind is not PartialFaultKind.PROTOCOL
        else PartialFaultKind.CRASH
    )
    wrong_kind = dataclasses.replace(
        fault,
        kind=replacement_kind,
        owner_connection=(
            None if replacement_kind is PartialFaultKind.INFRASTRUCTURE else 0
        ),
    )
    with pytest.raises(ReplayVerificationError, match="differs from captured"):
        _verify_payload(payload, fault=wrong_kind)

    wrong_hand = dataclasses.replace(fault, hand_number=2)
    with pytest.raises(ReplayVerificationError, match="final started hand"):
        _verify_payload(payload, fault=wrong_hand)

    at_deadline = copy.deepcopy(payload)
    request = next(
        item
        for item in at_deadline["events"]
        if item.get("hand") == 3 and item.get("type") == "action_requested"
    )
    action_event = next(
        item
        for item in at_deadline["events"]
        if item.get("hand") == 3 and item.get("action") == action
    )
    action_event["action_epoch_ms"] = request["deadline_epoch_ms"]
    action_event["action_monotonic_ns"] = request[
        "platform_deadline_monotonic_ns"
    ]
    action_event["decision_wait_sec"] = 60.0
    action_event["decision_wait_ms"] = 60_000
    action_event["decision_wait_ns"] = 60_000_000_000
    at_deadline["events_tail"] = at_deadline["events"][-20:]
    with pytest.raises(
        ReplayVerificationError,
        match="timeout attribution takes precedence",
    ):
        _verify_payload(at_deadline, fault=fault)


def test_illegal_action_counter_and_partial_fault_are_derived_from_events() -> None:
    payload = _captured_payload(3, timeout_on_last=True)
    timeout_event = next(
        event
        for event in payload["events"]
        if event.get("hand") == 3 and event.get("action") == "timeout"
    )
    timeout_event["action"] = "illegal:bet 5"
    timeout_event["reason"] = "unknown action 'bet'"
    timeout_event["snapshot_tier"] = "first-search"
    timeout_event["search_nodes"] = 100
    request_event = next(
        event
        for event in payload["events"]
        if event.get("hand") == 3 and event.get("type") == "action_requested"
    )
    timeout_event["compute_finished_monotonic_ns"] = (
        request_event["requested_monotonic_ns"] + 1_000_000
    )
    timeout_event["send_not_before_monotonic_ns"] = (
        timeout_event["compute_finished_monotonic_ns"]
        + BINDING.action_send_delay_ms * 1_000_000
    )
    timeout_event["action_epoch_ms"] = request_event["requested_epoch_ms"] + 1
    timeout_event["action_monotonic_ns"] = timeout_event[
        "send_not_before_monotonic_ns"
    ]
    timeout_event["decision_wait_ms"] = 1
    timeout_event["decision_wait_ns"] = 1_000_000
    timeout_event["decision_wait_sec"] = 0.001
    payload["per_player"]["BotA"]["timeouts"] = 0
    payload["per_player"]["BotA"]["illegal_actions"] = 1
    illegal_issue = "BotA: illegal_actions=1"
    payload["per_player"]["BotA"]["compliance_issues"] = [illegal_issue]
    payload["issues"] = [illegal_issue]
    payload["events_tail"] = payload["events"][-20:]
    fault = PartialReplayFault(
        kind=PartialFaultKind.ILLEGAL_ACTION,
        owner_connection=0,
        hand_number=3,
        evidence_digest=hashlib.sha256(b"illegal-action-evidence").hexdigest(),
    )

    verified = _verify_payload(payload, fault=fault)

    assert verified.timeout_count_by_connection == (0, 0)
    assert verified.illegal_action_count_by_connection == (1, 0)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda row: row.update(passed_compliance=False),
            "top-level compliance flag contradicts issues",
        ),
        (
            lambda row: row.update(issues=["fabricated issue"]),
            "top-level compliance flag contradicts issues",
        ),
        (
            lambda row: row["per_player"]["BotA"].update(
                passed_compliance=False
            ),
            "BotA compliance flag contradicts",
        ),
        (
            lambda row: row["per_player"]["BotA"]["native"].update(
                returncode=1,
                process_failures=1,
            ),
            "clean compliance contradicts native process evidence",
        ),
    ),
)
def test_clean_compliance_summary_tampering_fails_closed(mutation, message) -> None:
    payload = _captured_payload()
    mutation(payload)
    with pytest.raises(ReplayVerificationError, match=message):
        _verify_payload(payload)


def test_partial_fault_cannot_claim_clean_compliance_flags() -> None:
    payload = _captured_payload(3, timeout_on_last=True)
    payload["passed_compliance"] = True
    payload["issues"] = []
    payload["per_player"]["BotA"]["passed_compliance"] = True
    payload["per_player"]["BotA"]["compliance_issues"] = []
    fault = PartialReplayFault(
        kind=PartialFaultKind.TIMEOUT,
        owner_connection=0,
        hand_number=3,
        evidence_digest=hashlib.sha256(b"timeout-clean-lie").hexdigest(),
    )

    with pytest.raises(ReplayVerificationError, match="partial fault replay"):
        _verify_payload(payload, fault=fault)


def test_partial_fault_owner_rejects_boolean_connection_indices() -> None:
    with pytest.raises(ValueError, match="connection 0 or 1"):
        PartialReplayFault(
            kind=PartialFaultKind.CRASH,
            owner_connection=True,
            hand_number=1,
            evidence_digest=hashlib.sha256(b"bool-owner").hexdigest(),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda row: row.update(wrapper_used=True), "cannot use a wrapper"),
        (lambda row: row.update(execution_mode="adapter"), "execution_mode"),
        (lambda row: row.update(hands_requested=69), "hands_requested"),
        (
            lambda row: row["events"][23].update(sb_idx=0, bb_idx=1),
            "does not alternate",
        ),
        (
            lambda row: row["events"][2]["hole_cards"][0].__setitem__(
                0, "<0,0>"
            ),
            "frozen deck|repeat",
        ),
        (
            lambda row: row["events"][5].update(stage="turn"),
            "wrong stage|out of order",
        ),
        (
            lambda row: row["events"][3].update(pot=151),
            "request pot/bets differ",
        ),
        (
            lambda row: row["events"][4].update(amount=49),
            "call amount/pot differ",
        ),
        (
            lambda row: row["events"][7]["player_chips"].__setitem__(0, 19_899),
            "player_chips differ",
        ),
        (
            lambda row: row["events"][26].update(
                action="illegal:fold",
                reason="fabricated illegal",
            ),
            "independently legal action as illegal",
        ),
            (
                lambda row: row["events"][22].update(
                    earnings=[-100, 100],
                    winner_idx=1,
                ),
                "terminal utility",
        ),
        (
            lambda row: row["events"][22]["earnings"].__setitem__(1, 1),
            "not zero-sum",
        ),
        (
                lambda row: row["events"][22].update(winner_idx=1),
                "winner disagrees",
        ),
        (
            lambda row: row["events"][-1]["total_earnings"].__setitem__(0, 51),
            "match_end differs",
        ),
        (
            lambda row: row["per_player"]["BotA"].update(earnings=51),
            "earnings differ",
        ),
    ),
)
def test_tampered_replay_fails_closed(mutation, message) -> None:
    payload = _captured_payload()
    mutation(payload)
    payload["events_tail"] = payload["events"][-20:]
    with pytest.raises(ReplayVerificationError, match=message):
        _verify_payload(payload)


def test_event_tail_and_timeout_summary_are_cross_checked() -> None:
    payload = _captured_payload(3, timeout_on_last=True)
    fault = PartialReplayFault(
        kind=PartialFaultKind.TIMEOUT,
        owner_connection=0,
        hand_number=3,
        evidence_digest=hashlib.sha256(b"timeout-evidence").hexdigest(),
    )
    payload["per_player"]["BotA"]["timeouts"] = 0
    with pytest.raises(ReplayVerificationError, match="timeout count differs"):
        _verify_payload(payload, fault=fault)

    payload = _captured_payload()
    payload["events_tail"] = []
    with pytest.raises(ReplayVerificationError, match="events_tail differs"):
        _verify_payload(payload)


def test_connection_mapping_is_mandatory_ordered_and_cannot_be_grafted() -> None:
    missing = _captured_payload()
    missing["events"].pop(0)
    missing["events_tail"] = missing["events"][-20:]
    with pytest.raises(ReplayVerificationError, match="mandatory first client_order"):
        _verify_payload(missing)

    swapped = _captured_payload()
    swapped["events"][0]["connection_order"] = ["BotB", "BotA"]
    swapped["events_tail"] = swapped["events"][-20:]
    with pytest.raises(ReplayVerificationError, match="exact frozen connection mapping"):
        _verify_payload(swapped)

    other = _development_binding(
        leg_plan_digest=_h("other-leg"),
        connection_identity_digests=BINDING.connection_identity_digests,
        run_ids_by_connection=BINDING.run_ids_by_connection,
        process_tree_ids_by_connection=BINDING.process_tree_ids_by_connection,
        cgroup_paths_by_connection=BINDING.cgroup_paths_by_connection,
        resource_profile_digest=BINDING.resource_profile_digest,
    )
    with pytest.raises(ReplayVerificationError, match="captured execution binding"):
        _verify_payload(_captured_payload(), binding=other)


def test_wire_binding_and_binding_capability_fail_closed_on_copy_or_tamper() -> None:
    with pytest.raises(ReplayVerificationError, match="raw wire bytes differ"):
        _verify_payload(
            _captured_payload(),
            raw_wire=b"different-wire-capture",
        )

    changed_wire = _development_binding(
        leg_plan_digest=BINDING.leg_plan_digest,
        connection_identity_digests=BINDING.connection_identity_digests,
        run_ids_by_connection=BINDING.run_ids_by_connection,
        process_tree_ids_by_connection=BINDING.process_tree_ids_by_connection,
        cgroup_paths_by_connection=BINDING.cgroup_paths_by_connection,
        resource_profile_digest=BINDING.resource_profile_digest,
        raw_wire=b"different-wire-capture",
    )
    with pytest.raises(ReplayVerificationError, match="captured execution binding"):
        _verify_payload(
            _captured_payload(),
            binding=changed_wire,
            raw_wire=b"different-wire-capture",
        )

    copied = copy.copy(BINDING)
    with pytest.raises(TypeError, match="copied, forged, or altered"):
        copied._assert_bound()

    tampered = _development_binding()
    object.__setattr__(tampered, "binding_digest", _h("tampered-binding-digest"))
    with pytest.raises(TypeError, match="copied, forged, or altered"):
        tampered._assert_bound()


def test_action_timestamps_enforce_cell_budget_and_real_platform_timeout() -> None:
    over_budget = _captured_payload()
    request = next(
        event for event in over_budget["events"] if event.get("type") == "action_requested"
    )
    action = next(
        event for event in over_budget["events"] if event.get("type") == "action"
    )
    action["compute_finished_monotonic_ns"] = request[
        "compute_deadline_monotonic_ns"
    ]
    action["send_not_before_monotonic_ns"] = (
        action["compute_finished_monotonic_ns"]
        + BINDING.action_send_delay_ms * 1_000_000
    )
    action["action_monotonic_ns"] = action["send_not_before_monotonic_ns"] + 1
    action["action_epoch_ms"] = (
        request["requested_epoch_ms"] + BINDING.decision_budget_ms + 1
    )
    action["decision_wait_ns"] = (
        action["action_monotonic_ns"] - request["requested_monotonic_ns"]
    )
    action["decision_wait_ms"] = action["decision_wait_ns"] // 1_000_000
    action["decision_wait_sec"] = action["decision_wait_ns"] / 1_000_000_000
    over_budget["events_tail"] = over_budget["events"][-20:]
    with pytest.raises(ReplayVerificationError, match="missed its frozen cell budget"):
        _verify_payload(over_budget)

    fake_timeout = _captured_payload(3, timeout_on_last=True)
    timeout_request = next(
        event
        for event in fake_timeout["events"]
        if event.get("hand") == 3 and event.get("type") == "action_requested"
    )
    timeout_action = next(
        event
        for event in fake_timeout["events"]
        if event.get("hand") == 3 and event.get("action") == "timeout"
    )
    timeout_action["action_epoch_ms"] = timeout_request["requested_epoch_ms"]
    timeout_action["action_monotonic_ns"] = timeout_request[
        "requested_monotonic_ns"
    ]
    timeout_action["decision_wait_ms"] = 0
    timeout_action["decision_wait_ns"] = 0
    timeout_action["decision_wait_sec"] = 0.0
    fake_timeout["events_tail"] = fake_timeout["events"][-20:]
    fault = PartialReplayFault(
        kind=PartialFaultKind.TIMEOUT,
        owner_connection=0,
        hand_number=3,
        evidence_digest=_h("fake-zero-wait-timeout"),
    )
    with pytest.raises(ReplayVerificationError, match="before the platform deadline"):
        _verify_payload(fake_timeout, fault=fault)


def test_frozen_official_send_delay_and_match_finalization_are_event_derived() -> None:
    delayed_binding = _development_binding(action_send_delay_ms=300)
    delayed_payload = _captured_payload(binding=delayed_binding)
    delayed = _verify_payload(delayed_payload, binding=delayed_binding)
    assert delayed.action_send_delay_ms == 300

    action = next(
        event for event in delayed_payload["events"] if event.get("type") == "action"
    )
    action["send_not_before_monotonic_ns"] -= 1
    delayed_payload["events_tail"] = delayed_payload["events"][-20:]
    with pytest.raises(ReplayVerificationError, match="send throttle"):
        _verify_payload(delayed_payload, binding=delayed_binding)

    early_final = _captured_payload()
    early_final["events"][-1]["result_finalized_epoch_ms"] = 1
    early_final["events_tail"] = early_final["events"][-20:]
    with pytest.raises(ReplayVerificationError, match="finalization timestamp"):
        _verify_payload(early_final)


def test_duplicate_keys_nonfinite_utf8_size_and_nonbytes_are_rejected() -> None:
    with pytest.raises(ReplayVerificationError, match="duplicate JSON key"):
        verify_native_replay(
            b'{"x":1,"x":2}', COMMITMENT, execution_binding=BINDING, raw_wire=RAW_WIRE
        )
    with pytest.raises(ReplayVerificationError, match="non-finite"):
        verify_native_replay(
            b'{"x":NaN}', COMMITMENT, execution_binding=BINDING, raw_wire=RAW_WIRE
        )
    with pytest.raises(ReplayVerificationError, match="UTF-8"):
        verify_native_replay(
            b'{"x":"\xff"}', COMMITMENT, execution_binding=BINDING, raw_wire=RAW_WIRE
        )
    with pytest.raises(ReplayVerificationError, match="size"):
        verify_native_replay(
            b"x" * (MAX_NATIVE_REPLAY_BYTES + 1),
            COMMITMENT,
            execution_binding=BINDING,
            raw_wire=RAW_WIRE,
        )
    with pytest.raises(ReplayVerificationError, match="raw JSON bytes"):
        verify_native_replay(  # type: ignore[arg-type]
            "{}", COMMITMENT, execution_binding=BINDING, raw_wire=RAW_WIRE
        )


def test_raw_digest_preserves_bytes_while_canonical_digest_normalizes_json() -> None:
    payload = _captured_payload()
    canonical = _json_bytes(payload)
    spaced = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    first = verify_native_replay(
        canonical, COMMITMENT, execution_binding=BINDING, raw_wire=RAW_WIRE
    )
    second = verify_native_replay(
        spaced, COMMITMENT, execution_binding=BINDING, raw_wire=RAW_WIRE
    )

    assert first.raw_digest != second.raw_digest
    assert first.canonical_digest == second.canonical_digest
    assert first.event_digests == second.event_digests


def _formal_capture_payload(
    raw_replay: bytes,
    raw_wire: bytes,
    *,
    leg_plan: LegPlan,
    profile_digest: str,
) -> tuple[bytes, tuple[str, str]]:
    """Rewrite only the harness-owned binding/telemetry projection.

    Process and cgroup identities mirror the externally signed test fixture;
    the wire messages, tokenization, cards, actions, and settlements remain the
    real socket capture and are independently reverified later.
    """

    identities = leg_plan.connection_to_identity
    run_ids = (_h("run-0-pass"), _h("run-1-pass"))
    process_trees = ("pgid:5000", "pgid:5001")
    cgroups = (
        "/sys/fs/cgroup/pok-formal/leg/connection-0",
        "/sys/fs/cgroup/pok-formal/leg/connection-1",
    )
    binding = {
        "schema": "pok-native-replay-execution-binding-v1",
        "leg_plan_digest": leg_plan.digest(),
        "connection_identity_digests": list(identities),
        "run_ids_by_connection": list(run_ids),
        "process_tree_ids_by_connection": list(process_trees),
        "cgroup_paths_by_connection": list(cgroups),
        "resource_profile_digest": profile_digest,
        "decision_budget_ms": 5_000,
        "platform_action_timeout_ms": 60_000,
        "action_send_delay_ms": 0,
        "raw_wire_digest": hashlib.sha256(raw_wire).hexdigest(),
        "authority": "formal_enforcer_bound",
    }
    connection_bindings = tuple(
        hashlib.sha256(
            _json_bytes(
                {
                    "schema": "pok-native-replay-connection-binding-v1",
                    "connection_index": index,
                    "identity_digest": identities[index],
                    "run_id": run_ids[index],
                    "process_tree_id": process_trees[index],
                    "cgroup_path": cgroups[index],
                }
            )
        ).hexdigest()
        for index in range(2)
    )
    payload = json.loads(raw_replay)
    payload["execution_binding"] = binding
    client_order = next(
        event for event in payload["events"] if event.get("type") == "client_order"
    )
    client_order.update(
        {
            "connection_identity_digests": list(identities),
            "run_ids_by_connection": list(run_ids),
            "process_tree_ids_by_connection": list(process_trees),
            "cgroup_paths_by_connection": list(cgroups),
            "connection_binding_digests": list(connection_bindings),
        }
    )
    for event in payload["events"]:
        if event.get("type") != "action" or event.get("action") == "timeout":
            continue
        event.update(
            {
                "search_nodes": 0,
                "fallback_used": True,
                "snapshot_tier": "safe-fallback",
                "telemetry_source": "trusted_worker_trace",
                "compute_finished_monotonic_ns": event["action_monotonic_ns"],
                "send_not_before_monotonic_ns": event["action_monotonic_ns"],
            }
        )
    payload["events_tail"] = payload["events"][-20:]
    return _json_bytes(payload), identities


def _signed_decision_events(
    *,
    raw_replay: bytes,
    raw_wire: bytes,
) -> tuple[DecisionEnforcementEvent, ...]:
    projection = SimpleNamespace(raw_replay=raw_replay, raw_wire=raw_wire)
    rows = _decision_enforcement_events(projection)
    result: list[DecisionEnforcementEvent] = []
    for row in rows:
        requested = int(row["requested_monotonic_ns"])
        action_ingress = int(row["action_sent_monotonic_ns"])
        result.append(
            DecisionEnforcementEvent(
                **row,
                worker_thawed_monotonic_ns=requested,
                complete_snapshot_monotonic_ns=action_ingress,
                worker_frozen_monotonic_ns=action_ingress,
                compute_budget_ms=5_000,
                platform_timeout_ms=60_000,
                selected_snapshot_digest=_h(
                    f"formal-positive-snapshot-{row['decision_index']}"
                ),
                fallback_was_ready_at_request=True,
                opponent_worker_frozen=True,
                hard_stop_fired=False,
                fault_connection_index=None,
            )
        )
    return tuple(result)


def test_signed_supervisor_wire_replay_positive_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the production bridge while mocking only fixed host authority."""

    commitment = build_70_hand_commitment(
        int.from_bytes(hashlib.sha256(b"formal-supervisor-positive-root").digest(), "big")
    )
    capture = run_development_tcp_capture_sync(commitment, decision_budget_ms=5_000)
    evaluation_profile, _enforcement_profile = _profile()
    identities = (_h("formal-positive-identity-0"), _h("formal-positive-identity-1"))
    leg_plan = LegPlan(
        formal_plan_digest=_h("formal-positive-plan"),
        stratum_digest=_h("formal-positive-stratum"),
        block_id=_h("formal-positive-block"),
        block_plan_digest=_h("formal-positive-block-plan"),
        leg_index=0,
        connection_to_identity=identities,
        deal_sequence_digest=_h("formal-positive-deal-sequence"),
    )
    raw_replay, captured_identities = _formal_capture_payload(
        capture.raw_replay,
        capture.raw_wire,
        leg_plan=leg_plan,
        profile_digest=evaluation_profile.digest(),
    )
    assert captured_identities == identities
    replay_payload = json.loads(raw_replay)
    wire_semantics = verify_structured_wire_capture(
        capture.raw_wire,
        replay_payload,
    )
    decision_events = _signed_decision_events(
        raw_replay=raw_replay,
        raw_wire=capture.raw_wire,
    )
    fixture_root = tmp_path / "signed-supervisor"
    fixture_root.mkdir()
    bridge, _contract, _readiness, _launch, receipt, _ledger, _cleanup = (
        _authorized_supervisor_leg_fixture(
            monkeypatch,
            fixture_root,
            leg_plan_digest=leg_plan.digest(),
            profile_digest=evaluation_profile.digest(),
            ordered_identity_digests=identities,
            raw_wire_digest=hashlib.sha256(capture.raw_wire).hexdigest(),
            wire_semantic_digest=wire_semantics.semantic_binding_digest,
            replay_digest=hashlib.sha256(raw_replay).hexdigest(),
            decision_events=decision_events,
        )
    )
    context = bind_authorized_supervisor_replay(
        leg_plan=leg_plan,
        resource_profile=evaluation_profile,
        authorized_supervisor_leg=bridge,
        raw_wire=capture.raw_wire,
        raw_replay=raw_replay,
    )
    verified = verify_native_replay(
        raw_replay,
        commitment,
        execution_binding=context.execution_binding,
        raw_wire=capture.raw_wire,
    )
    assert verified.clean_complete
    assert verified.execution_binding_authority == "formal_enforcer_bound"
    assert verified.telemetry_complete_by_connection == (True, True)
    assert verified.supervisor_leg_receipt_digest == receipt.payload_digest()
    replay_receipt = ReplayVerificationReceipt.from_verified_native_replay(
        leg_plan=leg_plan,
        verified_replay=verified,
        issuer_digest=_h("formal-positive-replay-issuer"),
        rules_digest=_h("formal-positive-rules"),
        oracle_fixture_digest=_h("formal-positive-oracle"),
        adjudicated_fault=None,
    )
    replay_receipt._assert_formal_verifier_authority()
    assert replay_receipt.raw_replay_digest == hashlib.sha256(raw_replay).hexdigest()
    observation = MatchObservation(
        leg_plan=leg_plan,
        execution_receipts=context.execution_receipts,
        resource_receipts=context.resource_receipts,
        replay_receipt=replay_receipt,
        actual_dealt_prefix_digests=replay_receipt.actual_dealt_prefix_digests,
        actual_replay_digest=replay_receipt.raw_replay_digest,
        match_trace_digest=replay_receipt.match_trace_digest,
        verified_event_digests=replay_receipt.verified_event_digests,
        hands_started=replay_receipt.hands_started,
        hands_played=replay_receipt.hands_played,
        net_chips_connection0=replay_receipt.net_chips_connection0,
        match_wall_elapsed_ms=1,
        telemetry_by_connection=tuple(
            replay_receipt.derived_decision_telemetry(index)
            for index in range(2)
        ),
        timeout_count_by_connection=replay_receipt.timeout_count_by_connection,
        illegal_action_count_by_connection=(
            replay_receipt.illegal_action_count_by_connection
        ),
    )
    assert observation.supervisor_leg_receipt_digest == receipt.payload_digest()
    assert len(observation.observation_digest()) == 64

    with pytest.raises(
        (ValueError, FormalEnforcementUnavailable),
        match="already emitted|already consumed",
    ):
        bind_authorized_supervisor_replay(
            leg_plan=leg_plan,
            resource_profile=evaluation_profile,
            authorized_supervisor_leg=bridge,
            raw_wire=capture.raw_wire,
            raw_replay=raw_replay,
        )

    bad_events = list(decision_events)
    bad_events[0] = dataclasses.replace(
        bad_events[0],
        request_token_digest=_h("signed-but-wire-mismatched-request-token"),
    )
    bad_root = tmp_path / "signed-supervisor-bad-decision"
    bad_root.mkdir()
    bad_bridge, *_unused = _authorized_supervisor_leg_fixture(
        monkeypatch,
        bad_root,
        leg_plan_digest=leg_plan.digest(),
        profile_digest=evaluation_profile.digest(),
        ordered_identity_digests=identities,
        raw_wire_digest=hashlib.sha256(capture.raw_wire).hexdigest(),
        wire_semantic_digest=wire_semantics.semantic_binding_digest,
        replay_digest=hashlib.sha256(raw_replay).hexdigest(),
        decision_events=tuple(bad_events),
    )
    with pytest.raises(ValueError, match="not derived from exact wire records"):
        bind_authorized_supervisor_replay(
            leg_plan=leg_plan,
            resource_profile=evaluation_profile,
            authorized_supervisor_leg=bad_bridge,
            raw_wire=capture.raw_wire,
            raw_replay=raw_replay,
        )
