"""Tests for EventBroadcaster — SSE fan-out core."""

import asyncio

from web_ui import EventBroadcaster


class TestEventBroadcaster:
    def test_add_client_returns_id_and_queue(self):
        eb = EventBroadcaster()
        cid, q = eb.add_client()
        assert isinstance(cid, int)
        assert isinstance(q, asyncio.Queue)

    def test_client_ids_increment(self):
        eb = EventBroadcaster()
        cid1, _ = eb.add_client()
        cid2, _ = eb.add_client()
        assert cid2 == cid1 + 1

    def test_broadcast_delivers_to_client(self):
        eb = EventBroadcaster()
        cid, q = eb.add_client()
        eb.broadcast("test", {"msg": "hello"})
        event = q.get_nowait()
        assert event["event"] == "test"
        import json
        data = json.loads(event["data"])
        assert data["msg"] == "hello"
        assert "ts" in data

    def test_ring_buffer_replay(self):
        eb = EventBroadcaster()
        eb.broadcast("history", {"x": 1})
        eb.broadcast("history", {"x": 2})
        cid, q = eb.add_client()
        e1 = q.get_nowait()
        e2 = q.get_nowait()
        import json
        assert json.loads(e1["data"])["x"] == 1
        assert json.loads(e2["data"])["x"] == 2

    def test_remove_client_no_error(self):
        eb = EventBroadcaster()
        cid, _ = eb.add_client()
        eb.remove_client(cid)
        eb.broadcast("test", {"msg": "gone"})
        # Should not raise

    def test_remove_nonexistent_client_no_error(self):
        eb = EventBroadcaster()
        eb.remove_client(999)

    def test_clear_empties_buffer(self):
        eb = EventBroadcaster()
        eb.broadcast("a", {"x": 1})
        eb.broadcast("b", {"x": 2})
        eb.clear()
        _, q = eb.add_client()
        assert q.empty()

    def test_multiple_clients_all_receive(self):
        eb = EventBroadcaster()
        _, q1 = eb.add_client()
        _, q2 = eb.add_client()
        eb.broadcast("multi", {"v": 42})
        assert not q1.empty()
        assert not q2.empty()

    def test_ring_buffer_size_limit(self):
        eb = EventBroadcaster(buffer_size=3)
        for i in range(5):
            eb.broadcast("fill", {"i": i})
        _, q = eb.add_client()
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert len(events) == 3
        import json
        assert json.loads(events[-1]["data"])["i"] == 4
