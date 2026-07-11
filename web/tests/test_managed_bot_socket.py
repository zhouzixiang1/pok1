import json
from pathlib import Path
import select
import shutil
import socket
import subprocess

import pytest

from bot_artifact import hash_path
from managed_bot_socket import endpoint_environment
from official_bot_sandbox import build_sandboxed_bot_command, seal_bot_artifact
from official_execution_profile import load_execution_profile


def _listener() -> socket.socket:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


def test_endpoint_environment_requires_a_real_loopback_socket():
    peer, inherited = socket.socketpair()
    try:
        environment = endpoint_environment(inherited.fileno(), "127.0.0.1", 10001)
        assert environment["POK_PRECONNECTED_SOCKET_FD"] == str(inherited.fileno())
        with pytest.raises(ValueError, match="loopback"):
            endpoint_environment(inherited.fileno(), "8.8.8.8", 10001)
        with pytest.raises(ValueError, match="port"):
            endpoint_environment(inherited.fileno(), "127.0.0.1", 0)
    finally:
        peer.close()
        inherited.close()


def test_bwrap_bot_can_use_only_inherited_stream(tmp_path):
    profile = load_execution_profile()
    bwrap = Path(profile["tools"]["bwrap"]["command_path"])
    python = Path(profile["tools"]["python"]["command_path"])
    if not bwrap.is_file() or not python.is_file() or shutil.which(str(bwrap)) is None:
        pytest.skip("tracked formal Bubblewrap/Python profile is unavailable")

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
        "probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)\n"
        "probe.settimeout(0.25)\n"
        "try:\n"
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
    preconnected = socket.create_connection(
        ("127.0.0.1", authorized_port),
        timeout=2,
    )
    accepted, _peer = authorized_listener.accept()
    try:
        command, environment = build_sandboxed_bot_command(
            sealed,
            host="127.0.0.1",
            port=authorized_port,
            name="candidate",
            seat="upper",
            log_path=None,
            supports_log=False,
            preconnected_fd=preconnected.fileno(),
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            pass_fds=(preconnected.fileno(),),
        )
        preconnected.close()
        payload = accepted.recv(4096)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        report = json.loads(payload.decode())
        assert report == {
            "exec_inherited_wire": False,
            "inheritable": False,
            "second_endpoint_blocked": True,
        }
        readable, _, _ = select.select([forbidden_listener], [], [], 0.1)
        assert readable == []
        assert "--share-net" not in command
    finally:
        preconnected.close()
        accepted.close()
        authorized_listener.close()
        forbidden_listener.close()
