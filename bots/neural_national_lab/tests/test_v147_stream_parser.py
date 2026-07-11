from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import threading
import time


ROOT = Path(__file__).resolve().parents[3]
BOT_ENTRY = (
    ROOT
    / "bots"
    / "neural_national_lab"
    / "versions"
    / "v147_national_v146_streamsafe_tcp"
    / "national_bot.py"
)


def _load_entry():
    spec = importlib.util.spec_from_file_location("neural_v147_national_bot", BOT_ENTRY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _feed(module, parts: list[str]) -> tuple[list[str], str]:
    messages: list[str] = []
    buffer = ""
    for part in parts:
        buffer += part
        batch, buffer = module._split_messages(buffer, flush_numeric=False)
        messages.extend(batch)
    batch, buffer = module._split_messages(buffer, flush_numeric=True)
    messages.extend(batch)
    return messages, buffer


def test_numeric_tokens_wait_for_a_boundary() -> None:
    module = _load_entry()

    assert module._split_messages("raise 2", flush_numeric=False) == ([], "raise 2")
    assert module._split_messages("earnChips -1", flush_numeric=False) == ([], "earnChips -1")
    assert module._split_messages("call", flush_numeric=False) == (["call"], "")


def test_every_single_split_of_sticky_stream_is_lossless() -> None:
    module = _load_entry()
    payload = "earnChips -100preflop|SMALLBLIND|<0,3><1,12>raise 200call"
    expected = [
        "earnChips -100",
        "preflop|SMALLBLIND|<0,3><1,12>",
        "raise 200",
        "call",
    ]

    for split_at in range(1, len(payload)):
        messages, buffer = _feed(module, [payload[:split_at], payload[split_at:]])
        assert messages == expected, split_at
        assert buffer == ""


def test_recv_coalesces_numeric_fragment_before_dispatch() -> None:
    module = _load_entry()
    client, server = socket.socketpair()
    try:
        def send_fragments() -> None:
            server.sendall(b"raise 2")
            time.sleep(0.01)
            server.sendall(b"00call")

        sender = threading.Thread(target=send_fragments)
        sender.start()
        messages, buffer, closed = module._recv_messages(client, "")
        sender.join(timeout=1)

        assert messages == ["raise 200", "call"]
        assert buffer == ""
        assert closed is False
    finally:
        client.close()
        server.close()


def test_recv_flushes_standalone_numeric_message_after_quiet_window() -> None:
    module = _load_entry()
    client, server = socket.socketpair()
    try:
        server.sendall(b"earnChips -100")
        messages, buffer, closed = module._recv_messages(client, "")
        assert messages == ["earnChips -100"]
        assert buffer == ""
        assert closed is False
    finally:
        client.close()
        server.close()
