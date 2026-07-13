from __future__ import annotations

import json

import pytest

from ..decisionholdem_like import native_entry
from ..decisionholdem_like.blueprint import BlueprintTrainer
from ..decisionholdem_like.native_entry import (
    A2BlueprintClient,
    NationalStreamDecoder,
    _send_wire_action,
)


PRIVATE = [12, 25]
FLOP_MESSAGE = "flop|<0,0><1,3><2,7>"


def _client(tmp_path, name: str = "A2Test") -> A2BlueprintClient:
    trainer = BlueprintTrainer()
    trainer.train_to(2)
    blueprint = tmp_path / "blueprint.json"
    blueprint.write_text(
        json.dumps(trainer.blueprint_payload(), sort_keys=True),
        encoding="utf-8",
    )
    return A2BlueprintClient(name, str(blueprint), seed=19, log_path="")


def test_stream_decoder_preserves_fragmented_numeric_tokens_and_sticky_packets() -> None:
    decoder = NationalStreamDecoder()
    assert decoder.feed("earnChips -100preflop|SMALLBLIND|<0,3>") == [
        "earnChips -100"
    ]
    assert decoder.feed("<1,4>raise 20") == [
        "preflop|SMALLBLIND|<0,3><1,4>"
    ]
    assert decoder.has_pending_numeric
    assert decoder.feed("0call") == ["raise 200", "call"]
    assert decoder.buffer == ""


def test_preflop_closing_check_does_not_trigger_an_unsolicited_action(tmp_path) -> None:
    client = _client(tmp_path)
    client._new_hand("SMALLBLIND", PRIVATE)
    client._apply_hero_action("call")
    assert client.on_message("check") is None
    assert client.hero_action_count == 1


def test_new_street_infers_only_the_suppressed_peer_call_contribution(tmp_path) -> None:
    client = _client(tmp_path)
    client._new_hand("BIGBLIND", PRIVATE)
    client._apply_hero_action("raise 400")
    client.decide = lambda: "check"

    assert client.pot == 450
    assert client.opponent_bet == 50
    assert client.on_message(FLOP_MESSAGE) == "check"
    assert client.pot == 800
    assert client.opponent_chips == 19_600
    assert client.hero_bet == client.opponent_bet == 0


def test_called_allin_enters_runout_and_never_acts_on_later_streets(tmp_path) -> None:
    client = _client(tmp_path)
    client._new_hand("BIGBLIND", PRIVATE)
    client._apply_hero_action("allin")
    assert client.on_message("call") is None
    assert client.in_allin_runout
    client.decide = lambda: (_ for _ in ()).throw(AssertionError("must not decide"))
    assert client.on_message(FLOP_MESSAGE) is None
    assert client.on_message("turn|<3,8>") is None
    assert client.on_message("river|<0,9>") is None
    assert client.on_message("earnChips 20000") is None
    assert not client.in_allin_runout


def test_settlement_boundary_accounts_for_suppressed_call_but_not_a_fold(tmp_path) -> None:
    called = _client(tmp_path)
    called._new_hand("BIGBLIND", PRIVATE)
    called._apply_hero_action("raise 400")
    assert called.on_message("earnChips 400") is None
    assert called.pot == 800
    assert called.opponent_chips == 19_600

    folded = _client(tmp_path)
    folded._new_hand("BIGBLIND", PRIVATE)
    folded._apply_hero_action("raise 400")
    assert folded.on_message("fold") is None
    assert folded.on_message("earnChips 100") is None
    assert folded.pot == 450
    assert folded.opponent_chips == 19_950


def test_postflop_peer_check_uses_call_to_close_the_street(tmp_path) -> None:
    client = _client(tmp_path)
    client._new_hand("SMALLBLIND", PRIVATE)
    client._new_street("flop", [0, 16, 33])
    client.decide = lambda: "call"
    assert client.on_message("check") == "call"
    assert client.responding_to_check


class _Socket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)


def test_handshake_is_not_misclassified_when_bot_name_equals_a_wire_action(tmp_path) -> None:
    client = _client(tmp_path, name="call")
    client.action_delay = 0.0
    client._new_hand("SMALLBLIND", PRIVATE)
    sock = _Socket()
    _send_wire_action(sock, client, "call", is_handshake=True)
    assert sock.sent == [b"call"]
    assert client.hero_action_count == 0
    assert client.hero_bet == 50

    _send_wire_action(sock, client, "call")
    assert client.hero_action_count == 1
    assert client.hero_bet == 100


def test_decision_send_waits_for_official_delay_but_handshake_does_not(
    tmp_path,
    monkeypatch,
) -> None:
    client = _client(tmp_path, name="call")
    client.action_delay = 0.30
    client.last_platform_message_at = 10.0
    client._new_hand("SMALLBLIND", PRIVATE)
    sock = _Socket()
    sleeps: list[float] = []
    monkeypatch.setattr(native_entry.time, "monotonic", lambda: 10.1)
    monkeypatch.setattr(native_entry.time, "sleep", sleeps.append)

    _send_wire_action(sock, client, "call", is_handshake=True)
    assert sleeps == []
    _send_wire_action(sock, client, "call")
    assert sleeps == pytest.approx([0.2])
