from pathlib import Path
import importlib.util

from official_wire_probe import (
    OfficialWireReplay,
    replay_events,
    split_client_messages,
    split_server_messages,
)


ROOT = Path(__file__).resolve().parents[2]


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


def test_client_stream_split_preserves_illegal_raise_spacing_for_replay():
    messages, rest = split_client_messages("raise  200", allow_name=False)

    assert messages == []
    assert rest == "raise  200"


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


def test_replay_flags_platform_silent_settlement_gap_without_pending_bot_timeout():
    summary = replay_events([
        _event(0, "A", "server_to_bot", ["river|<0,1>"]),
        _event(1, "A", "bot_to_server", ["raise 100"]),
        _event(2, "A", "server_to_bot", ["call"]),
        _event(64, "A", "server_to_bot", ["earnChips 200"]),
    ])

    assert summary["issues"][0]["kind"] == "platform_silent_timeout_gap"
    assert summary["issues"][0]["waited_sec"] >= 62
    assert summary["max_platform_silent_gap_sec"] >= 62
    assert summary["pending_expected_actions"] == []


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


def test_sample_wrapper_patches_hardcoded_server_address():
    spec = importlib.util.spec_from_file_location("run_national_sample", ROOT / "scripts" / "run_national_sample.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    patched = module._patched_source('server_address = ("47.98.125.65", 10001)\n', host="127.0.0.1", port=23456)

    assert "server_address = ('127.0.0.1', 23456)" in patched


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
    assert module._target_reached({
        "hands_started_min": 70,
        "settlements_min": 69,
        "pending_expected_actions": [],
    }, 70)
