import json
import select
import socket

import pytest

from bot_artifact import hash_path
from managed_bot_executor import EndpointLease, EndpointLeaseError
from official_bot_sandbox import launch_sandboxed_bot, seal_bot_artifact


def _listener() -> socket.socket:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


def test_endpoint_lease_rejects_unix_and_non_loopback_authority():
    peer, inherited = socket.socketpair()
    try:
        with pytest.raises(EndpointLeaseError, match="family"):
            EndpointLease.adopt(
                inherited,
                peer_host="127.0.0.1",
                peer_port=10001,
            )
    finally:
        peer.close()
        inherited.close()
    with pytest.raises(EndpointLeaseError, match="loopback"):
        EndpointLease.connect("8.8.8.8", 10001, timeout=0.1)


def test_bwrap_bot_can_use_only_inherited_stream(tmp_path):
    authorized_listener = _listener()
    forbidden_listener = _listener()
    authorized_port = authorized_listener.getsockname()[1]
    forbidden_port = forbidden_listener.getsockname()[1]
    source = tmp_path / "national_v1"
    source.mkdir()
    (source / "national_bot.py").write_text(
        "import _socket, json, os, socket, subprocess, sys\n"
        f"authorized_port = {authorized_port}\n"
        f"forbidden_port = {forbidden_port}\n"
        "wire = socket.create_connection(('127.0.0.1', authorized_port), timeout=2)\n"
        "report = {'inheritable': os.get_inheritable(wire.fileno())}\n"
        "try:\n"
        "    probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)\n"
        "    probe.settimeout(0.25)\n"
        "    probe.connect(('127.0.0.1', forbidden_port))\n"
        "except OSError:\n"
        "    report['second_endpoint_blocked'] = True\n"
        "else:\n"
        "    report['second_endpoint_blocked'] = False\n"
        "child = subprocess.run([sys.executable, '-I', '-c', "
        "    f\"import os; print(os.get_inheritable({wire.fileno()}))\"], "
        "    capture_output=True, text=True, check=False)\n"
        "report['exec_inherited_wire'] = child.returncode == 0\n"
        "wire.sendall(json.dumps(report, sort_keys=True).encode())\n"
        "wire.close()\n",
        encoding="utf-8",
    )
    sealed = seal_bot_artifact(
        source,
        tmp_path / "sealed" / "candidate",
        expected_hash=hash_path(source),
    )
    try:
        with EndpointLease.connect(
            "127.0.0.1", authorized_port, timeout=2
        ) as endpoint:
            accepted, _peer = authorized_listener.accept()
            managed = launch_sandboxed_bot(
                sealed,
                endpoint,
                name="candidate",
                seat="upper",
                log_path=None,
                supports_log=False,
            )
        payload = accepted.recv(4096)
        stdout, stderr = managed.process.communicate(timeout=10)
        assert managed.process.returncode == 0, (stdout, stderr)
        report = json.loads(payload.decode())
        assert report == {
            "exec_inherited_wire": False,
            "inheritable": False,
            "second_endpoint_blocked": True,
        }
        readable, _, _ = select.select([forbidden_listener], [], [], 0.1)
        assert readable == []
        assert managed.isolation.bpf_size > 0
    finally:
        if "accepted" in locals():
            accepted.close()
        authorized_listener.close()
        forbidden_listener.close()
