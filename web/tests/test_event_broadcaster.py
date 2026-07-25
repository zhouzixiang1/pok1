"""Tests for EventBroadcaster — SSE fan-out core."""

import asyncio

from web_ui import EventBroadcaster


AUTHORITY_A = "a" * 64
AUTHORITY_B = "b" * 64


def _broadcaster():
    value = EventBroadcaster()
    value.bind_authority(AUTHORITY_A)
    return value


def _stream_authority(*, high_water, active_bots, state="strict_published"):
    from epoch_authority import epoch_stream_authority_digest

    return epoch_stream_authority_digest({
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": state,
        "initialized": True,
        "reset_receipt_valid": True,
        "reset_receipt_digest": "c" * 64,
        "version_authority_high_water": high_water,
        "active_bots": active_bots,
    })


class TestEventBroadcaster:
    def test_add_client_returns_id_and_queue(self):
        eb = _broadcaster()
        cid, q = eb.add_client(AUTHORITY_A)
        assert isinstance(cid, int)
        assert isinstance(q, asyncio.Queue)

    def test_client_ids_increment(self):
        eb = _broadcaster()
        cid1, _ = eb.add_client(AUTHORITY_A)
        cid2, _ = eb.add_client(AUTHORITY_A)
        assert cid2 == cid1 + 1

    def test_broadcast_delivers_to_client(self):
        eb = _broadcaster()
        cid, q = eb.add_client(AUTHORITY_A)
        eb.broadcast("test", {"msg": "hello"})
        event = q.get_nowait()
        assert event["event"] == "test"
        import json
        data = json.loads(event["data"])
        assert data["msg"] == "hello"
        assert "ts" in data

    def test_ring_buffer_replay(self):
        eb = _broadcaster()
        eb.broadcast("history", {"x": 1})
        eb.broadcast("history", {"x": 2})
        cid, q = eb.add_client(AUTHORITY_A)
        e1 = q.get_nowait()
        e2 = q.get_nowait()
        import json
        assert json.loads(e1["data"])["x"] == 1
        assert json.loads(e2["data"])["x"] == 2

    def test_remove_client_no_error(self):
        eb = _broadcaster()
        cid, _ = eb.add_client(AUTHORITY_A)
        eb.remove_client(cid)
        eb.broadcast("test", {"msg": "gone"})
        # Should not raise

    def test_remove_nonexistent_client_no_error(self):
        eb = _broadcaster()
        eb.remove_client(999)

    def test_clear_empties_buffer(self):
        eb = _broadcaster()
        eb.broadcast("a", {"x": 1})
        eb.broadcast("b", {"x": 2})
        eb.clear()
        _, q = eb.add_client(AUTHORITY_A)
        assert q.empty()

    def test_multiple_clients_all_receive(self):
        eb = _broadcaster()
        _, q1 = eb.add_client(AUTHORITY_A)
        _, q2 = eb.add_client(AUTHORITY_A)
        eb.broadcast("multi", {"v": 42})
        assert not q1.empty()
        assert not q2.empty()

    def test_ring_buffer_size_limit(self):
        eb = EventBroadcaster(buffer_size=3)
        eb.bind_authority(AUTHORITY_A)
        for i in range(5):
            eb.broadcast("fill", {"i": i})
        _, q = eb.add_client(AUTHORITY_A)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert len(events) == 3
        import json
        assert json.loads(events[-1]["data"])["i"] == 4

    def test_authority_change_clears_ring_and_fences_old_client_delivery(self):
        eb = _broadcaster()
        _, old_queue = eb.add_client(AUTHORITY_A)
        eb.broadcast("history", {"identity": "a"})
        assert not old_queue.empty()
        old_queue.get_nowait()

        eb.bind_authority(AUTHORITY_B)
        eb.broadcast("history", {"identity": "b"})
        _, new_queue = eb.add_client(AUTHORITY_B)

        assert old_queue.empty()
        replay = new_queue.get_nowait()
        import json
        assert json.loads(replay["data"])["identity"] == "b"

    def test_client_cannot_subscribe_or_replay_with_wrong_authority(self):
        import pytest

        eb = _broadcaster()
        eb.broadcast("history", {"identity": "a"})
        with pytest.raises(ValueError, match="does not match"):
            eb.add_client(AUTHORITY_B)

    def test_publication_under_same_reset_receipt_replaces_replay_identity(self):
        from bot_namespace import bot_name
        from conftest import STRICT_SOURCE_V, STRICT_TARGET_V

        before = _stream_authority(
            high_water=STRICT_SOURCE_V,
            active_bots=[],
            state="fresh_bootstrap_ready",
        )
        after = _stream_authority(
            high_water=STRICT_TARGET_V,
            active_bots=[bot_name(STRICT_TARGET_V)],
        )
        assert before is not None
        assert after is not None
        assert before != after

        eb = EventBroadcaster()
        eb.bind_authority(before)
        eb.broadcast("history", {"generation": STRICT_SOURCE_V})
        eb.bind_authority(after)
        eb.broadcast("history", {"generation": STRICT_TARGET_V})
        _, queue = eb.add_client(after)

        import json
        replay = json.loads(queue.get_nowait()["data"])
        assert replay["generation"] == STRICT_TARGET_V
        assert queue.empty()

    def test_delayed_old_request_cannot_roll_back_new_authority(self):
        eb = _broadcaster()
        old_request_expected = eb.authority_identity()

        assert eb.compare_and_bind_authority(
            AUTHORITY_B,
            expected_identity=AUTHORITY_A,
        ) is True
        assert eb.compare_and_bind_authority(
            AUTHORITY_A,
            expected_identity=old_request_expected,
        ) is False
        assert eb.authority_identity() == AUTHORITY_B

        _, queue = eb.add_client(AUTHORITY_B)
        eb.broadcast("history", {"identity": "new"})
        assert not queue.empty()
