import asyncio

import pytest

from national_transport import NationalProtocolError, NationalTCPClient, pop_client_action


def test_action_parser_requires_terminal_boundary_for_variable_tokens():
    assert pop_client_action("raise 2", terminal=False) == (None, "raise 2")
    assert pop_client_action("raise 200", terminal=True) == ("raise 200", "")
    assert pop_client_action("raise 200call", terminal=False) == (
        "raise 200",
        "call",
    )


def test_action_parser_preserves_officially_illegal_whitespace_and_bet():
    assert pop_client_action(" raise 200", terminal=True) == (" raise 200", "")
    assert pop_client_action("raise 200 ", terminal=True) == ("raise 200 ", "")
    assert pop_client_action("bet 200", terminal=True) == ("bet 200", "")


async def _receive_fragments(fragments, *, name=False, eof=False):
    class MemoryWriter:
        def write(self, _payload):
            return None

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

        def get_extra_info(self, _name):
            return None

    reader = asyncio.StreamReader()
    client = NationalTCPClient(reader, MemoryWriter(), idle_flush_sec=0.03)

    async def feed():
        for payload, delay in fragments:
            reader.feed_data(payload)
            await asyncio.sleep(delay)
        if eof:
            reader.feed_eof()

    feeder = asyncio.create_task(feed())
    try:
        return await asyncio.wait_for(
            client.recv_name(1.0) if name else client.recv_line(1.0),
            timeout=2.0,
        )
    finally:
        await feeder
        await client.close()


def test_transport_reassembles_raise_split_without_newline():
    result = asyncio.run(_receive_fragments([
        (b"rai", 0.002),
        (b"se 2", 0.002),
        (b"00", 0.04),
    ]))
    assert result == "raise 200"


def test_transport_rejects_multiple_unsolicited_actions_in_one_packet():
    result = asyncio.run(_receive_fragments([(b"callfold", 0.04)]))
    assert result == "protocol_multiple_actions:call|fold"


def test_transport_preserves_split_utf8_team_name():
    encoded = "国赛原生队".encode("utf-8")
    result = asyncio.run(_receive_fragments([
        (encoded[:5], 0.002),
        (encoded[5:], 0.04),
    ], name=True))
    assert result == "国赛原生队"


def test_transport_maps_invalid_utf8_to_illegal_action():
    result = asyncio.run(_receive_fragments([(b"\xff\xfe", 0.04)]))
    assert result == "protocol_error:client_invalid_utf8"


def test_transport_rejects_truncated_utf8_at_eof():
    async def scenario():
        with pytest.raises(NationalProtocolError, match="client_invalid_utf8"):
            await _receive_fragments(
                [(b"Team\xe4\xb8", 0.0)],
                name=True,
                eof=True,
            )

    asyncio.run(scenario())
