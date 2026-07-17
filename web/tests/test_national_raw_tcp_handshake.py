import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from national_native import NATIVE_BOT_TEMPLATE
from national_runtime_telemetry import parse_native_bot_log


SLOW_POLICY = '''\
import time

# Deliberately slow worker import: the raw TCP name reply must not wait for it.
time.sleep(4.0)


def get_baseline_decision(context):
    return {"kind": "pass"}


def iter_decisions(context, baseline, deadline):
    if False:
        yield baseline
'''


def _read_until(sock: socket.socket, expected: bytes, timeout: float) -> tuple[bytes, float]:
    deadline = time.monotonic() + timeout
    payload = bytearray()
    first_received_at = None
    while len(payload) < len(expected):
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"timed out waiting for {expected!r}; got {bytes(payload)!r}"
        sock.settimeout(remaining)
        chunk = sock.recv(4096)
        assert chunk, f"peer closed before {expected!r}; got {bytes(payload)!r}"
        if first_received_at is None:
            first_received_at = time.monotonic()
        payload.extend(chunk)
    assert first_received_at is not None
    return bytes(payload), first_received_at


def test_generated_native_bot_replies_to_raw_name_before_slow_worker_import(tmp_path):
    """Exercise the exact generated executable over one delimiter-free socket.

    This is intentionally not an in-process ``handle`` unit test.  The server
    writes ``name`` immediately followed by the first preflop frame, so a
    runtime that waits for policy import/readiness before its name response
    misses the bounded handshake observation even though an eventual decision
    could still look locally healthy.
    """

    bot_dir = tmp_path / "generated"
    bot_dir.mkdir()
    entry = bot_dir / "national_bot.py"
    log_path = bot_dir / "bot.log"
    entry.write_text(NATIVE_BOT_TEMPLATE, encoding="utf-8")
    (bot_dir / "policy.py").write_text(SLOW_POLICY, encoding="utf-8")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    environment = dict(os.environ)
    environment["POK_OFFICIAL_ACTION_DELAY"] = "0"
    process = subprocess.Popen(
        [
            sys.executable,
            str(entry),
            "--host",
            str(host),
            "--port",
            str(port),
            "--name",
            "RawStrict",
            "--log",
            str(log_path),
        ],
        cwd=str(bot_dir),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    connection = None
    try:
        listener.settimeout(10.0)
        connection, _address = listener.accept()
        started = time.monotonic()
        # No delimiter exists between either protocol token.
        connection.sendall(b"namepreflop|SMALLBLIND|<0,12><1,11>")
        # The input deliberately includes an immediate preflop frame, but this
        # test owns only the name/startup boundary.  The system fallback may
        # legally choose fold or pass for this minimally framed preflop state;
        # pinning one strategy action would turn a raw-TCP timing regression
        # into an accidental policy-strength assertion.
        expected = b"RawStrict"
        wire, first_reply_at = _read_until(connection, expected, timeout=12.0)
        assert wire.startswith(expected)
        assert b"\n" not in wire and b"\r" not in wire
        # The worker intentionally spends four seconds importing policy.  A
        # name reply received first proves no ready/import wait sits before the
        # official raw handshake, even with concurrent CPU load.
        assert first_reply_at - started < 3.0
    finally:
        if connection is not None:
            connection.close()
        listener.close()
        try:
            stdout, stderr = process.communicate(timeout=12.0)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5.0)
            raise AssertionError(
                "generated native bot did not close after raw test: "
                f"stdout={stdout[-1000:]!r} stderr={stderr[-1000:]!r}"
            )

    assert process.returncode == 0, stderr
    telemetry = parse_native_bot_log(log_path.read_text(encoding="utf-8"))
    handshake = telemetry["name_handshake"]
    assert handshake["available"] is True
    assert handshake["received_count"] == 1
    assert handshake["sent_count"] == 1
    assert handshake["worker_launch_started_count"] == 1
    assert handshake["worker_launch_ok_count"] == 1
    assert handshake["worker_launch_failed_count"] == 0
    assert handshake["worker_generations"] == [1]
    assert handshake["malformed_count"] == 0
