from __future__ import annotations

import threading
import random

import pytest

from bots.research_native_lab.common_contracts.actions import ActionKind
from bots.research_native_lab.common_contracts.protocol import (
    NationalProtocolSession,
    ProtocolDecodeError,
    ProtocolStateError,
    StreamDecoder,
)
from bots.research_native_lab.common_contracts.actions import Action
from bots.research_native_lab.common_contracts.national_state import NationalGameState


TOKENS = [
    "name",
    "preflop|SMALLBLIND|<0,12><1,11>",
    "raise 200",
    "call",
    "flop|<2,0><3,1><0,2>",
    "check",
    "call",
    "turn|<1,3>",
    "raise 100",
    "fold",
    "earnChips -100",
    "preflop|BIGBLIND|<0,4><1,5>",
]


def test_stream_decoder_handles_every_single_split_and_sticky_packets() -> None:
    wire = "".join(TOKENS)
    for split in range(len(wire) + 1):
        decoder = StreamDecoder()
        actual = decoder.feed(wire[:split]) + decoder.feed(wire[split:])
        actual += decoder.finish()
        assert actual == TOKENS, split

    rng = random.Random(2026071205)
    for _ in range(500):
        cuts = sorted(set(rng.randrange(len(wire) + 1) for _ in range(20)))
        decoder = StreamDecoder()
        actual = []
        start = 0
        for end in cuts + [len(wire)]:
            actual.extend(decoder.feed(wire[start:end]))
            start = end
        actual.extend(decoder.finish())
        assert actual == TOKENS


def test_stream_decoder_does_not_truncate_numeric_recv_boundary() -> None:
    decoder = StreamDecoder()
    assert decoder.feed("raise 2") == []
    assert decoder.feed("00call") == ["raise 200", "call"]
    assert decoder.feed("earnChips -10") == []
    assert decoder.flush_numeric() == ["earnChips -10"]


def test_numeric_quiet_flush_keeps_partial_non_numeric_tokens() -> None:
    decoder = StreamDecoder()
    assert decoder.feed("prefl") == []
    assert decoder.flush_numeric() == []
    assert decoder.feed("op|SMALLBLIND|<0,1><1") == []
    assert decoder.flush_numeric() == []
    assert decoder.feed(",2>") == ["preflop|SMALLBLIND|<0,1><1,2>"]
    assert decoder.feed("cal") == []
    assert decoder.flush_numeric() == []
    assert decoder.feed("l") == ["call"]


def test_decoder_rejects_invalid_utf8_and_enforces_buffer_cap() -> None:
    with pytest.raises(ProtocolDecodeError, match="ASCII"):
        StreamDecoder().feed(b"\xff")
    with pytest.raises(ProtocolDecodeError, match="safety limit"):
        StreamDecoder(max_buffer_bytes=4).feed("xxxxx")


def test_stream_decoder_rejects_incomplete_or_unknown_tail() -> None:
    decoder = StreamDecoder()
    decoder.feed("flop|<0,1><1,2>")
    with pytest.raises(ProtocolDecodeError):
        decoder.finish()


def test_session_reconstructs_suppressed_preflop_close_and_showdown() -> None:
    session = NationalProtocolSession("ResearchA")
    assert session.receive("name").kind == "name_requested"
    start = session.receive("preflop|SMALLBLIND|<0,12><1,11>")
    decision = start.payload["decision_id"]
    session.submit_action(decision, "call")

    # Official EXE may omit the peer BB check and jump to the flop.
    flop = session.receive("flop|<2,0><3,1><0,2>")
    assert flop.payload["inferred_closing_action"] == "check"
    session.receive("check")
    session.submit_action(session.pending_decision_id, "call")
    session.receive("turn|<1,3>")
    session.receive("check")
    session.submit_action(session.pending_decision_id, "call")
    session.receive("river|<2,4>")
    session.receive("check")
    session.submit_action(session.pending_decision_id, "call")

    assert session.current.is_terminal
    opponent = (20, 21)
    expected = session.current.terminal_utility(hole_cards=(session.current.hole_cards[0], opponent))[0]
    session.receive(f"earnChips {expected}")
    showdown = session.receive("oppo_hands|<0,5><1,5>")
    assert showdown.kind == "showdown"
    assert showdown.payload["terminal_utility"][0] == expected
    with pytest.raises(ProtocolStateError, match="duplicate opponent"):
        session.receive("oppo_hands|<0,5><1,5>")


def test_session_infers_only_opponent_closing_action_at_stage_boundary() -> None:
    session = NationalProtocolSession("ResearchB")
    session.receive("preflop|BIGBLIND|<0,12><1,11>")
    event = session.receive("call")
    session.submit_action(event.payload["decision_id"], "check")
    flop = session.receive("flop|<2,0><3,1><0,2>")
    session.submit_action(flop.payload["decision_id"], "check")
    turn = session.receive("turn|<1,3>")
    assert turn.payload["inferred_closing_action"] == "call"
    assert session.current.street.value == "turn"
    assert session.has_pending_decision


def test_session_rejects_unsolicited_duplicate_and_non_owner_actions() -> None:
    session = NationalProtocolSession("ResearchC")
    start = session.receive("preflop|SMALLBLIND|<0,12><1,11>")
    ticket = start.payload["decision_id"]
    session.submit_action(ticket, "fold")
    with pytest.raises(ProtocolStateError):
        session.submit_action(ticket, "call")

    failures = []

    def wrong_thread() -> None:
        try:
            session.name_response()
        except Exception as exc:  # noqa: BLE001 - asserted below
            failures.append(exc)

    thread = threading.Thread(target=wrong_thread)
    thread.start()
    thread.join()
    assert len(failures) == 1
    assert isinstance(failures[0], ProtocolStateError)


def test_official_hand70_shape_requires_independent_thp_proof() -> None:
    session = NationalProtocolSession("ResearchD")
    session.hands_started = 70
    session.settlements_received = 69
    session.current = NationalGameState.new_hand(70, small_blind=0).apply_action(
        Action(ActionKind.FOLD)
    )
    evidence = session.connection_close_evidence()
    assert evidence["natural_70_boundary"]
    assert evidence["requires_thp_state_69"]
    assert not evidence["wire_alone_proves_complete"]


def test_session_rejects_early_or_impossible_settlement() -> None:
    session = NationalProtocolSession("AdversarialEarn")
    session.receive("preflop|BIGBLIND|<0,12><1,11>")
    with pytest.raises(ProtocolStateError, match="before a terminal"):
        session.receive("earnChips 100")
    with pytest.raises(ProtocolStateError, match="stack range"):
        # Make the hand terminal first so range validation is the failing gate.
        session.current = session.current.apply_action(Action(ActionKind.FOLD))
        session.receive("earnChips 20001")


def test_showdown_before_earn_is_cross_checked_when_earn_arrives() -> None:
    session = NationalProtocolSession("ShowdownFirst")
    start = session.receive("preflop|SMALLBLIND|<0,12><1,11>")
    session.submit_action(start.payload["decision_id"], "call")
    session.receive("flop|<2,0><3,1><0,2>")
    session.receive("check")
    session.submit_action(session.pending_decision_id, "call")
    session.receive("turn|<1,3>")
    session.receive("check")
    session.submit_action(session.pending_decision_id, "call")
    session.receive("river|<2,4>")
    session.receive("check")
    session.submit_action(session.pending_decision_id, "call")
    showdown = session.receive("oppo_hands|<0,5><1,5>")
    expected = showdown.payload["terminal_utility"][0]
    with pytest.raises(ProtocolStateError, match="showdown utility"):
        session.receive(f"earnChips {expected + 1}")


def test_new_hand_requires_showdown_disclosure_and_blinds_alternate() -> None:
    session = NationalProtocolSession("Lifecycle")
    start = session.receive("preflop|SMALLBLIND|<0,12><1,11>")
    session.submit_action(start.payload["decision_id"], "fold")
    session.receive("earnChips -50")
    with pytest.raises(ProtocolStateError, match="blind role did not alternate"):
        session.receive("preflop|SMALLBLIND|<0,10><1,9>")

    # A fresh session reaches showdown and receives earn but not oppo_hands;
    # the next hand cannot silently discard that missing disclosure.
    showdown_session = NationalProtocolSession("MissingDisclosure")
    start = showdown_session.receive("preflop|SMALLBLIND|<0,12><1,11>")
    showdown_session.submit_action(start.payload["decision_id"], "allin")
    showdown_session.receive("call")
    showdown_session.receive("flop|<2,0><3,1><0,2>")
    showdown_session.receive("turn|<1,3>")
    showdown_session.receive("river|<2,4>")
    showdown_session.receive("earnChips 20000")
    with pytest.raises(ProtocolStateError, match="terminal evidence"):
        showdown_session.receive("preflop|BIGBLIND|<0,10><1,9>")


def test_strict_action_spacing_is_not_sanitized() -> None:
    session = NationalProtocolSession("ResearchE")
    start = session.receive("preflop|SMALLBLIND|<0,12><1,11>")
    for invalid in ("raise  200", "raise\t200", " raise 200", "raise 200 "):
        with pytest.raises(ValueError):
            session.submit_action(start.payload["decision_id"], invalid)
    assert session.has_pending_decision
    assert session.current.street_actions == ()
