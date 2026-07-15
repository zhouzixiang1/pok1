import ctypes.util
import errno
import json
import os
from pathlib import Path
import select
import socket

import pytest

import managed_bot_executor as executor
from managed_bot_executor import (
    BotTiming,
    EndpointLease,
    EndpointLeaseError,
    IsolationUnavailable,
    ManagedExecutorError,
    launch_isolated_worker,
    launch_managed_bot,
    managed_executor_identity,
    probe_managed_executor,
)


_NAMESPACES = ("user", "ipc", "pid", "net", "uts", "cgroup")


def _listener(family=socket.AF_INET) -> socket.socket:
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0) if family == socket.AF_INET else ("::1", 0))
    listener.listen(4)
    listener.settimeout(5.0)
    return listener


def _connected_pair() -> tuple[socket.socket, socket.socket, socket.socket]:
    listener = _listener()
    client = socket.create_connection(listener.getsockname(), timeout=2.0)
    accepted, _peer = listener.accept()
    return listener, client, accepted


def _receive_line(sock: socket.socket) -> bytes:
    data = bytearray()
    sock.settimeout(8.0)
    while b"\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data).partition(b"\n")[0]


def test_endpoint_lease_rejects_wrong_family_type_peer_and_socket_error():
    unix_a, unix_b = socket.socketpair()
    try:
        with pytest.raises(EndpointLeaseError, match="family"):
            EndpointLease.adopt(
                unix_b, peer_host="127.0.0.1", peer_port=10001
            )
    finally:
        unix_a.close()
        unix_b.close()

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(EndpointLeaseError, match="SOCK_STREAM"):
            EndpointLease.adopt(
                udp, peer_host="127.0.0.1", peer_port=10001
            )
    finally:
        udp.close()

    unconnected = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(EndpointLeaseError, match="already be connected"):
            EndpointLease.adopt(
                unconnected, peer_host="127.0.0.1", peer_port=10001
            )
    finally:
        unconnected.close()

    listener, client, accepted = _connected_pair()
    port = int(listener.getsockname()[1])
    try:
        with pytest.raises(EndpointLeaseError, match="authorized peer"):
            EndpointLease.adopt(
                client, peer_host="127.0.0.1", peer_port=port + 1
            )
        with pytest.raises(EndpointLeaseError, match="loopback"):
            EndpointLease.adopt(client, peer_host="8.8.8.8", peer_port=port)
    finally:
        client.close()
        accepted.close()
        listener.close()

    class PendingErrorSocket(socket.socket):
        def getsockopt(self, level, option, *args):
            if level == socket.SOL_SOCKET and option == socket.SO_ERROR:
                return errno.ECONNRESET
            return super().getsockopt(level, option, *args)

    listener, client, accepted = _connected_pair()
    wrapped = PendingErrorSocket(
        family=socket.AF_INET,
        type=socket.SOCK_STREAM,
        fileno=client.detach(),
    )
    try:
        with pytest.raises(EndpointLeaseError, match="SO_ERROR"):
            EndpointLease.adopt(
                wrapped,
                peer_host="127.0.0.1",
                peer_port=int(listener.getsockname()[1]),
            )
    finally:
        wrapped.close()
        accepted.close()
        listener.close()


def test_endpoint_lease_accepts_connected_ipv6_loopback_stream():
    try:
        listener = _listener(socket.AF_INET6)
    except OSError as exc:
        if exc.errno == errno.EADDRNOTAVAIL:
            pytest.skip("host has no configured IPv6 loopback address")
        raise
    client = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    client.settimeout(2.0)
    client.connect(listener.getsockname())
    client.settimeout(None)
    accepted, _peer = listener.accept()
    lease = EndpointLease.adopt(
        client,
        peer_host="::1",
        peer_port=int(listener.getsockname()[1]),
    )
    try:
        assert lease.peer == ("::1", int(listener.getsockname()[1]))
        assert lease.family == socket.AF_INET6
        assert lease.consumed is False
    finally:
        lease.close()
        accepted.close()
        listener.close()
    assert lease.closed is True


def test_generic_worker_has_real_kernel_isolation_and_only_named_outputs(
    tmp_path, monkeypatch
):
    work = tmp_path / "trusted-worker"
    work.mkdir()
    readonly_bot = tmp_path / "candidate"
    readonly_bot.mkdir()
    (readonly_bot / "value.txt").write_text("bound-input", encoding="utf-8")
    host_key = tmp_path / "host-secret.key"
    host_key.write_text("must-not-be-readable", encoding="utf-8")
    monkeypatch.setenv("HOST_SECRET", "ambient-secret")
    host_namespaces = {
        name: os.readlink(f"/proc/self/ns/{name}") for name in _NAMESPACES
    }
    worker = work / "worker.py"
    worker.write_text(
        r'''
import ctypes
import errno
import json
import os
import resource
import socket
import sys

host_key = sys.argv[1]
host_namespaces = json.loads(sys.argv[2])
status = {}
for line in open("/proc/self/status", encoding="utf-8"):
    key, separator, value = line.partition(":")
    if separator:
        status[key] = value.strip()
try:
    open(host_key, encoding="utf-8").read()
except OSError:
    host_key_unreadable = True
else:
    host_key_unreadable = False
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
except OSError as exc:
    inet_socket_errno = exc.errno
else:
    inet_socket_errno = 0
left, right = socket.socketpair()
left.close()
right.close()
try:
    open("/inputs/bot/value.txt", "w", encoding="utf-8").write("bad")
except OSError:
    input_readonly = True
else:
    input_readonly = False
try:
    open("/output/extra", "w", encoding="utf-8").write("bad")
except OSError:
    extra_output_blocked = True
else:
    extra_output_blocked = False
libc = ctypes.CDLL(None, use_errno=True)
ctypes.set_errno(0)
unshare_result = libc.unshare(0x10000000)
report = {
    "ambient_secret_absent": "HOST_SECRET" not in os.environ,
    "extra_output_blocked": extra_output_blocked,
    "host_key_unreadable": host_key_unreadable,
    "inet_socket_errno": inet_socket_errno,
    "input": open("/inputs/bot/value.txt", encoding="utf-8").read(),
    "input_readonly": input_readonly,
    "limits": {
        name: list(resource.getrlimit(getattr(resource, name)))
        for name in (
            "RLIMIT_NOFILE", "RLIMIT_NPROC", "RLIMIT_AS",
            "RLIMIT_FSIZE", "RLIMIT_CORE"
        )
    },
    "namespaces_differ": all(
        os.readlink("/proc/self/ns/" + name) != host_namespaces[name]
        for name in host_namespaces
    ),
    "status": {
        key: value for key, value in status.items()
        if key.startswith("Cap") or key in {"NoNewPrivs", "Seccomp"}
    },
    "unshare_errno": ctypes.get_errno(),
    "unshare_result": unshare_result,
    "visible": os.environ.get("VISIBLE"),
}
open("/output/report.json", "w", encoding="utf-8").write(
    json.dumps(report, sort_keys=True)
)
open("/output/phase.txt", "w", encoding="utf-8").write("complete\n")
''',
        encoding="utf-8",
    )
    report_path = tmp_path / "outputs" / "report.json"
    phase_path = tmp_path / "outputs" / "phase.txt"

    launched = launch_isolated_worker(
        work,
        [
            "/usr/bin/python3",
            "-I",
            "-B",
            "/work/worker.py",
            str(host_key),
            json.dumps(host_namespaces, sort_keys=True),
        ],
        environment={"VISIBLE": "yes"},
        readonly_inputs={"bot": readonly_bot},
        output_files={
            "report.json": report_path,
            "phase.txt": phase_path,
        },
    )
    stdout, stderr = launched.process.communicate(timeout=10.0)

    assert launched.process.returncode == 0, (stdout, stderr)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert phase_path.read_text(encoding="utf-8") == "complete\n"
    assert report["host_key_unreadable"] is True
    assert report["ambient_secret_absent"] is True
    assert report["inet_socket_errno"] == errno.EPERM
    assert report["input"] == "bound-input"
    assert report["input_readonly"] is True
    assert report["extra_output_blocked"] is True
    assert report["namespaces_differ"] is True
    assert report["unshare_result"] == -1
    assert report["unshare_errno"] == errno.EPERM
    assert report["visible"] == "yes"
    assert report["status"]["NoNewPrivs"] == "1"
    assert report["status"]["Seccomp"] == "2"
    assert all(
        value == "0000000000000000"
        for key, value in report["status"].items()
        if key.startswith("Cap")
    )
    assert report["limits"] == {
        "RLIMIT_AS": [4_294_967_296, 4_294_967_296],
        "RLIMIT_CORE": [0, 0],
        "RLIMIT_FSIZE": [16_777_216, 16_777_216],
        "RLIMIT_NOFILE": [64, 64],
        "RLIMIT_NPROC": [64, 64],
    }
    assert set(path.name for path in (report_path.parent).iterdir()) == {
        "report.json",
        "phase.txt",
    }
    assert launched.isolation.resource_limits == (
        ("RLIMIT_NOFILE", 64),
        ("RLIMIT_NPROC", 64),
        ("RLIMIT_AS", 4_294_967_296),
        ("RLIMIT_FSIZE", 16_777_216),
        ("RLIMIT_CORE", 0),
    )


def test_owner_start_barrier_binds_command_fd_and_exact_environment(monkeypatch):
    captured = {}

    class FakeProcess:
        pid = 424_242
        returncode = None

        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            marker = command.index("--block-fd")
            self.barrier_reader = os.dup(int(command[marker + 1]))

        def poll(self):
            return self.returncode

    monkeypatch.setattr(executor.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        executor.Path,
        "read_bytes",
        lambda path: (
            b"POK_MANAGED_PROCESS_OWNER=owner-1\x00"
            if path == Path("/proc/424242/environ")
            else pytest.fail(f"unexpected read: {path}")
        ),
    )
    program_read, program_write = os.pipe()
    inherited_read, inherited_write = os.pipe()
    os.close(program_write)
    closed = []
    program = executor._SeccompProgram(
        fd=program_read,
        bpf_sha256="a" * 64,
        policy_sha256="b" * 64,
        size=1,
    )
    try:
        launched = executor._spawn(
            ("/usr/bin/bwrap", "--die-with-parent", "/usr/bin/true"),
            program=program,
            inherited_fds=(inherited_read,),
            close_callbacks=(lambda: closed.append(True),),
            host_process_owner="owner-1",
            start_new_session=True,
        )
        command = captured["command"]
        barrier_fd = int(command[2])
        assert command == (
            "/usr/bin/bwrap",
            "--block-fd",
            str(barrier_fd),
            "--die-with-parent",
            "/usr/bin/true",
        )
        assert set(captured["kwargs"]["pass_fds"]) == {
            program_read,
            inherited_read,
            barrier_fd,
        }
        assert captured["kwargs"]["env"] == {
            "POK_MANAGED_PROCESS_OWNER": "owner-1"
        }
        assert captured["kwargs"]["start_new_session"] is True
        assert os.read(launched.process.barrier_reader, 1) == b"1"
        assert program.fd == -1
        assert closed == [True]
    finally:
        os.close(inherited_read)
        os.close(inherited_write)
        if "launched" in locals():
            os.close(launched.process.barrier_reader)


def test_owner_start_barrier_mismatch_terminates_reaps_and_cleans(monkeypatch):
    captured = {}

    class FakeProcess:
        pid = 434_343
        returncode = None
        terminated = False
        waited = False

        def __init__(self, command, **kwargs):
            captured["process"] = self
            marker = command.index("--block-fd")
            self.barrier_reader = os.dup(int(command[marker + 1]))

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            self.waited = True
            return self.returncode

    monkeypatch.setattr(executor.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        executor.Path,
        "read_bytes",
        lambda _path: b"UNEXPECTED=authority\x00",
    )
    program_read, program_write = os.pipe()
    os.close(program_write)
    closed = []
    program = executor._SeccompProgram(
        fd=program_read,
        bpf_sha256="c" * 64,
        policy_sha256="d" * 64,
        size=1,
    )
    with pytest.raises(ManagedExecutorError, match="verification failed"):
        executor._spawn(
            ("/usr/bin/bwrap", "/usr/bin/true"),
            program=program,
            close_callbacks=(lambda: closed.append(True),),
            host_process_owner="owner-2",
        )
    process = captured["process"]
    try:
        assert process.terminated is True
        assert process.waited is True
        assert os.read(process.barrier_reader, 1) == b""
        assert program.fd == -1
        assert closed == [True]
    finally:
        os.close(process.barrier_reader)


def test_spawn_without_owner_does_not_add_start_barrier(monkeypatch):
    captured = {}

    class FakeProcess:
        pid = 444_444

        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs

    monkeypatch.setattr(executor.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        executor.Path,
        "read_bytes",
        lambda _path: pytest.fail("owner environment must not be inspected"),
    )
    program_read, program_write = os.pipe()
    os.close(program_write)
    program = executor._SeccompProgram(
        fd=program_read,
        bpf_sha256="e" * 64,
        policy_sha256="f" * 64,
        size=1,
    )
    launched = executor._spawn(
        ("/usr/bin/bwrap", "/usr/bin/true"),
        program=program,
    )
    assert launched.process.pid == 444_444
    assert captured["command"] == ("/usr/bin/bwrap", "/usr/bin/true")
    assert captured["kwargs"]["pass_fds"] == (program_read,)
    assert captured["kwargs"]["env"] == {}
    assert program.fd == -1


def test_managed_bot_uses_one_exact_stream_log_and_clean_environment(
    tmp_path, monkeypatch
):
    authorized = _listener()
    forbidden = _listener()
    authorized_port = int(authorized.getsockname()[1])
    forbidden_port = int(forbidden.getsockname()[1])
    lease = EndpointLease.connect("127.0.0.1", authorized_port, timeout=2.0)
    accepted, _peer = authorized.accept()
    accepted.settimeout(8.0)
    host_key = tmp_path / "operator.key"
    host_key.write_text("operator-only", encoding="utf-8")
    monkeypatch.setenv("HOST_SECRET", "ambient")
    monkeypatch.setenv("POK_NATIVE_BOT_SEED", "ambient-seed")
    artifact = tmp_path / "bot"
    artifact.mkdir()
    (artifact / "national_bot.py").write_text(
        r'''
import _socket
import argparse
import ctypes
import json
import os
import socket

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--port", required=True, type=int)
parser.add_argument("--name", required=True)
parser.add_argument("--seat")
parser.add_argument("--log")
parser.add_argument("--forbidden-port", required=True, type=int)
parser.add_argument("--host-key", required=True)
args = parser.parse_args()
wire = socket.create_connection((args.host, args.port), timeout=2.0)
class Sockaddr(ctypes.Structure):
    _fields_ = [('family', ctypes.c_ushort), ('data', ctypes.c_ubyte * 14)]
disconnect_address = Sockaddr()
disconnect_address.family = socket.AF_UNSPEC
libc = ctypes.CDLL(None, use_errno=True)
ctypes.set_errno(0)
reconnect_result = libc.connect(
    wire.fileno(), ctypes.byref(disconnect_address), ctypes.sizeof(disconnect_address)
)
reconnect_errno = ctypes.get_errno()
try:
    _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
except OSError as exc:
    inet_socket_errno = exc.errno
else:
    inet_socket_errno = 0
try:
    socket.create_connection((args.host, args.forbidden_port), timeout=0.2)
except OSError:
    unauthorized_create_connection_blocked = True
else:
    unauthorized_create_connection_blocked = False
try:
    open(args.host_key, encoding="utf-8").read()
except OSError:
    host_key_unreadable = True
else:
    host_key_unreadable = False
status = {}
for line in open("/proc/self/status", encoding="utf-8"):
    key, separator, value = line.partition(":")
    if separator:
        status[key] = value.strip()
report = {
    "action_delay": os.environ.get("POK_OFFICIAL_ACTION_DELAY"),
    "arena_session": os.environ.get("POK_ARENA_SESSION_ID"),
    "baseline": os.environ.get("POK_DECISION_BASELINE_TARGET_SEC"),
    "fd_inheritable": os.get_inheritable(wire.fileno()),
    "hard_deadline": os.environ.get("POK_DECISION_HARD_DEADLINE_SEC"),
    "host_key_unreadable": host_key_unreadable,
    "host_secret_absent": "HOST_SECRET" not in os.environ,
    "host_owner_absent": "POK_MANAGED_PROCESS_OWNER" not in os.environ,
    "inet_socket_errno": inet_socket_errno,
    "name": args.name,
    "reconnect_errno": reconnect_errno,
    "reconnect_result": reconnect_result,
    "refinement": os.environ.get("POK_DECISION_REFINEMENT_BUDGET_SEC"),
    "seat": args.seat,
    "seed_absent": "POK_NATIVE_BOT_SEED" not in os.environ,
    "status": {
        key: value for key, value in status.items()
        if key.startswith("Cap") or key in {"NoNewPrivs", "Seccomp"}
    },
    "trace": os.environ.get("POK_TRACE_DECISIONS"),
    "unauthorized_create_connection_blocked": unauthorized_create_connection_blocked,
}
if args.log:
    open(args.log, "a", encoding="utf-8").write("one-decision\n")
wire.sendall((json.dumps(report, sort_keys=True) + "\n").encode("utf-8"))
wire.recv(1)
wire.close()
''',
        encoding="utf-8",
    )
    decision_log = tmp_path / "evidence" / "decision.log"

    launched = launch_managed_bot(
        artifact,
        lease,
        name="candidate",
        seat="upper",
        decision_log=decision_log,
        timing=BotTiming(
            action_delay=0.3,
            hard_deadline=7.0,
            refinement_budget=6.0,
            baseline_target=0.2,
        ),
        environment={
            "POK_ARENA_SESSION_ID": "session-1",
            "POK_TRACE_DECISIONS": "1",
        },
        extra_args=(
            "--forbidden-port",
            str(forbidden_port),
            "--host-key",
            str(host_key),
        ),
        start_new_session=True,
        host_process_owner="arena-session-owner-1",
    )
    assert os.getsid(launched.process.pid) == launched.process.pid
    host_environment = Path(f"/proc/{launched.process.pid}/environ").read_bytes()
    assert host_environment == b"POK_MANAGED_PROCESS_OWNER=arena-session-owner-1\x00"
    report = json.loads(_receive_line(accepted).decode("utf-8"))
    accepted.sendall(b"1")
    stdout, stderr = launched.process.communicate(timeout=10.0)

    assert launched.process.returncode == 0, (stdout, stderr)
    assert report["fd_inheritable"] is False
    assert report["inet_socket_errno"] == errno.EPERM
    assert report["reconnect_result"] == -1
    assert report["reconnect_errno"] == errno.EPERM
    assert report["unauthorized_create_connection_blocked"] is True
    assert report["host_key_unreadable"] is True
    assert report["host_secret_absent"] is True
    assert report["host_owner_absent"] is True
    assert report["seed_absent"] is True
    assert report["name"] == "candidate"
    assert report["seat"] == "upper"
    assert report["arena_session"] == "session-1"
    assert report["trace"] == "1"
    assert report["action_delay"] == "0.3"
    assert report["hard_deadline"] == "7.0"
    assert report["refinement"] == "6.0"
    assert report["baseline"] == "0.2"
    assert report["status"]["NoNewPrivs"] == "1"
    assert report["status"]["Seccomp"] == "2"
    assert all(
        value == "0000000000000000"
        for key, value in report["status"].items()
        if key.startswith("Cap")
    )
    assert decision_log.read_text(encoding="utf-8") == "one-decision\n"
    assert lease.consumed is True
    assert lease.closed is True
    readable, _, _ = select.select([forbidden], [], [], 0.1)
    assert readable == []
    with pytest.raises(EndpointLeaseError, match="already consumed"):
        launch_managed_bot(artifact, lease, name="second-consumer")

    accepted.close()
    authorized.close()
    forbidden.close()


def test_generic_worker_supports_threads_and_process_pool(tmp_path):
    work = tmp_path / "parallel-worker"
    work.mkdir()
    worker = work / "worker.py"
    worker.write_text(
        r'''
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import json
import os
import threading

def square(value):
    return os.getpid(), value * value

def main():
    direct = []
    thread = threading.Thread(target=lambda: direct.append("thread-ok"))
    thread.start()
    thread.join(timeout=3.0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        threaded = list(pool.map(lambda value: value + 1, (1, 2)))
    with ProcessPoolExecutor(max_workers=2) as pool:
        processed = list(pool.map(square, (3, 4)))
    payload = {
        "direct": direct,
        "threaded": threaded,
        "processed": processed,
        "worker_pid": os.getpid(),
    }
    open("/output/report.json", "w", encoding="utf-8").write(
        json.dumps(payload, sort_keys=True)
    )

if __name__ == "__main__":
    main()
''',
        encoding="utf-8",
    )
    report_path = tmp_path / "outputs" / "parallel.json"

    launched = launch_isolated_worker(
        work,
        ["/usr/bin/python3", "-I", "-B", "/work/worker.py"],
        output_files={"report.json": report_path},
    )
    stdout, stderr = launched.process.communicate(timeout=15.0)

    assert launched.process.returncode == 0, (stdout, stderr)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["direct"] == ["thread-ok"]
    assert report["threaded"] == [2, 3]
    assert [row[1] for row in report["processed"]] == [9, 16]
    assert all(row[0] != report["worker_pid"] for row in report["processed"])


def test_managed_bot_explicit_seed_and_environment_policy(tmp_path):
    listener = _listener()
    port = int(listener.getsockname()[1])
    lease = EndpointLease.connect("127.0.0.1", port, timeout=2.0)
    accepted, _peer = listener.accept()
    artifact = tmp_path / "bot"
    artifact.mkdir()
    (artifact / "national_bot.py").write_text(
        "import argparse,json,os,socket\n"
        "p=argparse.ArgumentParser();"
        "p.add_argument('--host');p.add_argument('--port',type=int);"
        "p.add_argument('--name');a=p.parse_args()\n"
        "s=socket.create_connection((a.host,a.port));"
        "s.sendall((json.dumps({'seed':os.environ.get('POK_NATIVE_BOT_SEED'),"
        "'hash':os.environ.get('PYTHONHASHSEED')})+'\\n').encode());s.close()\n",
        encoding="utf-8",
    )
    launched = launch_managed_bot(artifact, lease, name="seeded", seed=42)
    report = json.loads(_receive_line(accepted).decode("utf-8"))
    stdout, stderr = launched.process.communicate(timeout=8.0)
    assert launched.process.returncode == 0, (stdout, stderr)
    assert report == {"hash": "42", "seed": "42"}
    accepted.close()
    listener.close()

    listener = _listener()
    lease = EndpointLease.connect(
        "127.0.0.1", int(listener.getsockname()[1]), timeout=2.0
    )
    accepted, _peer = listener.accept()
    try:
        with pytest.raises(ManagedExecutorError, match="not allowlisted"):
            launch_managed_bot(
                artifact,
                lease,
                name="bad-env",
                environment={"HOME": "/host"},
            )
        assert lease.consumed is False
        with pytest.raises(ManagedExecutorError, match="host process owner"):
            launch_managed_bot(
                artifact,
                lease,
                name="bad-owner",
                host_process_owner="",
            )
        assert lease.consumed is False
    finally:
        lease.close()
        accepted.close()
        listener.close()


def test_managed_bot_rejects_stdlib_shadow_before_consuming_endpoint(tmp_path):
    listener = _listener()
    port = int(listener.getsockname()[1])
    lease = EndpointLease.connect("127.0.0.1", port, timeout=2.0)
    accepted, _peer = listener.accept()
    artifact = tmp_path / "bot-shadow"
    artifact.mkdir()
    (artifact / "national_bot.py").write_text("pass\n", encoding="utf-8")
    (artifact / "argparse.py").write_text(
        "raise SystemExit('candidate stdlib shadow executed')\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(
            ManagedExecutorError,
            match="managed_bot_stdlib_shadow_forbidden:argparse.py:argparse",
        ):
            launch_managed_bot(artifact, lease, name="shadowed")
        assert lease.consumed is False
    finally:
        lease.close()
        accepted.close()
        listener.close()


def test_executor_identity_and_real_probe_bind_the_executable_policy():
    identity = managed_executor_identity()
    assert identity["schema"] == "pok-managed-executor-identity-v3"
    assert identity["contract"]["host_process_environment"] == (
        "optional-owner-marker-block-fd-verified-before-release"
    )
    assert identity["contract"]["host_root_mounted"] is False
    assert identity["contract"]["namespaces"] == list(_NAMESPACES)
    assert identity["contract"]["writable_outputs"] == (
        "named-new-file-bind-fd-only"
    )
    assert identity["contract"]["managed_bot_sources"] == (
        "content-bound-top-level-files-sealed-before-spawn"
    )
    assert identity["contract"]["managed_bot_source_mount"] == (
        "sealed-memfd-ro-bind-data-only"
    )
    assert identity["contract"]["managed_bot_python_flags"] == ["-I", "-B"]
    for row in (
        identity["source"],
        identity["seccomp"],
        identity["seccomp"]["library"],
        *identity["tools"].values(),
    ):
        key = "bpf_sha256" if "bpf_sha256" in row else "sha256"
        assert len(row[key]) == 64

    report = probe_managed_executor()
    assert report["ok"] is True, report
    assert report["issues"] == []
    assert report["observed"]["inet_socket_errno"] == errno.EPERM
    assert report["observed"]["reconnect_result"] == -1
    assert report["observed"]["reconnect_errno"] == errno.EPERM
    assert report["observed"]["fd_inheritable"] is False
    assert report["observed"]["lease_consumed"] is True
    assert report["observed"]["lease_closed"] is True
    assert all(
        report["observed"]["namespaces"][name]
        != report["host_namespaces"][name]
        for name in _NAMESPACES
    )


def test_executor_identity_fails_closed_without_libseccomp(monkeypatch):
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    with pytest.raises(IsolationUnavailable, match="libseccomp_unavailable"):
        managed_executor_identity()


def test_output_contract_rejects_existing_or_overlapping_authority(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    existing = tmp_path / "existing.json"
    existing.write_text("do-not-overwrite", encoding="utf-8")
    with pytest.raises(ManagedExecutorError, match="new safe path"):
        launch_isolated_worker(
            work,
            ["/usr/bin/python3", "-c", "pass"],
            output_files={"report.json": existing},
        )
    assert existing.read_text(encoding="utf-8") == "do-not-overwrite"

    nested = work / "nested"
    nested.mkdir()
    with pytest.raises(ManagedExecutorError, match="non-overlapping"):
        launch_isolated_worker(
            work,
            ["/usr/bin/python3", "-c", "pass"],
            readonly_inputs={"nested": nested},
        )
