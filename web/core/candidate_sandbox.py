"""Central fail-closed sandbox for executing untrusted candidate Python.

Candidate artifacts are LLM-produced input.  Syntax/AST inspection may read
them on the host, but importing or running them must go through this boundary.
The managed executor supplies the actual isolation contract: a read-only
artifact mount, private user/pid/network namespaces, seccomp, resource limits,
a cleared environment, and a bounded tmpfs as the only anonymous writable
filesystem.

This module deliberately has no unsandboxed subprocess fallback.  If bwrap,
libseccomp, or another required primitive is unavailable, callers receive an
infrastructure exception and an authoritative gate must fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from typing import Mapping, Sequence

from managed_bot_executor import (
    IsolationIdentity,
    ManagedExecutorError,
    launch_isolated_worker,
)


_MAX_CAPTURE_BYTES = 1024 * 1024


class CandidateSandboxError(RuntimeError):
    """The candidate could not be executed under the mandatory isolation."""


class CandidateSandboxTimeout(CandidateSandboxError):
    """The isolated candidate exceeded its host-enforced wall-clock timeout."""


@dataclass(frozen=True)
class CandidateSandboxResult:
    returncode: int
    stdout: str
    stderr: str
    isolation: IsolationIdentity
    # This bit is derived only from the exact bytes in a fresh host-owned
    # completion file.  Candidate stdout/stderr are deliberately not authority:
    # imported candidate code shares those streams with the probe harness and
    # can print arbitrary look-alike JSON before terminating the interpreter.
    trusted_completion: bool = False


_TRUSTED_COMPLETION_SCHEMA = "candidate-probe-completion-v1"


def _completion_payload(token: str) -> bytes:
    return f"{_TRUSTED_COMPLETION_SCHEMA}:{token}\n".encode("ascii")


def _trusted_probe_wrapper(source: str, token_pipe_fd: int) -> str:
    """Return the system-owned probe wrapper.

    The candidate-facing source executes first.  Only control flow which
    returns normally reaches the write to the separately captured completion
    file.  In particular, candidate output cannot forge this channel and
    ``os._exit(0)`` kills the sandbox before the receipt is written.

    A fresh nonce is generated inside the trusted main thread, sent to the host
    over a write-only anonymous pipe, and retained only in that thread's local
    frame.  Candidate code runs in a different thread after the pipe is closed;
    the wrapper removes Python's cross-thread frame accessor first.  Thus the
    candidate can see the completion *file* but not the nonce needed to forge
    its exact bytes.

    Keep the syscall callables in wrapper locals before candidate execution.
    A candidate may replace attributes on the shared ``os`` module, but doing
    so cannot redirect the already-bound completion operations.
    """
    return (
        "import os as _trusted_os\n"
        "import secrets as _trusted_secrets\n"
        "import sys as _trusted_sys\n"
        "import threading as _trusted_threading\n"
        "_trusted_open = _trusted_os.open\n"
        "_trusted_write = _trusted_os.write\n"
        "_trusted_fsync = _trusted_os.fsync\n"
        "_trusted_close = _trusted_os.close\n"
        f"_candidate_probe_source = {source!r}\n"
        f"_trusted_token_pipe_fd = {int(token_pipe_fd)}\n"
        "def _trusted_write_all(_fd, _payload):\n"
        "    _view = memoryview(_payload)\n"
        "    while _view:\n"
        "        _count = _trusted_write(_fd, _view)\n"
        "        if _count <= 0:\n"
        "            raise RuntimeError('trusted completion write failed')\n"
        "        _view = _view[_count:]\n"
        "def _trusted_main():\n"
        "    _token = _trusted_secrets.token_hex(32)\n"
        "    _trusted_write_all(_trusted_token_pipe_fd, (_token + '\\n').encode('ascii'))\n"
        "    _trusted_close(_trusted_token_pipe_fd)\n"
        "    for _name in (\n"
        "        '_current_frames', '_current_exceptions',\n"
        "        '_settraceallthreads', '_setprofileallthreads',\n"
        "    ):\n"
        "        if hasattr(_trusted_sys, _name):\n"
        "            delattr(_trusted_sys, _name)\n"
        "    for _name in ('settrace_all_threads', 'setprofile_all_threads'):\n"
        "        if hasattr(_trusted_threading, _name):\n"
        "            delattr(_trusted_threading, _name)\n"
        "    def _trusted_audit(_event, _args):\n"
        "        if _event in {'sys.settrace', 'sys.setprofile'}:\n"
        "            raise RuntimeError('candidate cross-thread inspection denied')\n"
        "        if _event == 'import' and _args and _args[0] in {\n"
        "            'ctypes', '_ctypes', 'cffi', '_cffi_backend',\n"
        "        }:\n"
        "            raise RuntimeError('candidate native introspection denied')\n"
        "        if _event in {'ctypes.dlopen', 'ctypes.dlsym'}:\n"
        "            raise RuntimeError('candidate native introspection denied')\n"
        "    _trusted_sys.addaudithook(_trusted_audit)\n"
        "    _outcome = []\n"
        "    def _candidate_target():\n"
        "        _candidate_probe_globals = {\n"
        "            '__name__': '__main__',\n"
        "            '__file__': '/inputs/harness/probe.py',\n"
        "            '__builtins__': __builtins__,\n"
        "        }\n"
        "        try:\n"
        "            exec(compile(_candidate_probe_source, '<candidate-probe>', 'exec'), "
        "_candidate_probe_globals, _candidate_probe_globals)\n"
        "        except BaseException as _error:\n"
        "            _outcome.append(_error)\n"
        "        else:\n"
        "            _outcome.append(None)\n"
        "    _thread = _trusted_threading.Thread(target=_candidate_target, daemon=False)\n"
        "    _thread.start()\n"
        "    _thread.join()\n"
        "    if len(_outcome) != 1 or _outcome[0] is not None:\n"
        "        if len(_outcome) == 1:\n"
        "            _error = _outcome[0]\n"
        "            _diagnostic = ('candidate probe error: ' + type(_error).__name__ "
        "+ ': ' + str(_error) + '\\n').encode('utf-8', errors='replace')\n"
        "            _trusted_write_all(2, _diagnostic[:800])\n"
        "            raise _outcome[0]\n"
        "        raise RuntimeError('candidate probe completion state invalid')\n"
        "    _payload = ("
        f"{_TRUSTED_COMPLETION_SCHEMA!r} + ':' + _token + '\\n').encode('ascii')\n"
        "    _flags = _trusted_os.O_WRONLY | _trusted_os.O_TRUNC\n"
        "    _flags |= int(getattr(_trusted_os, 'O_CLOEXEC', 0))\n"
        "    _flags |= int(getattr(_trusted_os, 'O_NOFOLLOW', 0))\n"
        "    _fd = _trusted_open('/output/trusted_completion', _flags)\n"
        "    try:\n"
        "        _trusted_write_all(_fd, _payload)\n"
        "        _trusted_fsync(_fd)\n"
        "    finally:\n"
        "        _trusted_close(_fd)\n"
        "_trusted_main()\n"
    )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Reap the entire isolated launch, including candidate subprocesses."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired as exc:
        raise CandidateSandboxError(
            "candidate_sandbox_process_group_unreapable"
        ) from exc


def _decode(value: bytes | None) -> str:
    return (value or b"").decode("utf-8", errors="replace")


def _read_capped(handle) -> str:
    handle.flush()
    size = int(os.fstat(handle.fileno()).st_size)
    start = max(0, size - _MAX_CAPTURE_BYTES)
    handle.seek(start)
    content = handle.read(_MAX_CAPTURE_BYTES)
    prefix = b"[candidate output truncated]\n" if start else b""
    return _decode(prefix + content)


def run_candidate_python(
    artifact_root: str | Path,
    argv: Sequence[str],
    *,
    timeout: float,
    input_text: str | None = None,
    environment: Mapping[str, object] | None = None,
    readonly_inputs: Mapping[str, str | Path] | None = None,
    _trusted_completion: tuple[str | Path, int, int] | None = None,
) -> CandidateSandboxResult:
    """Run Python against a read-only candidate artifact under one boundary.

    ``argv`` is the argument vector following ``/usr/bin/python3``.  Absolute
    in-sandbox paths must use ``/work`` for the candidate and ``/inputs/<name>``
    for explicitly supplied trusted inputs.  No host path is visible inside the
    child.
    """

    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CandidateSandboxError("candidate_sandbox_timeout_invalid") from exc
    if not 0.05 <= timeout_value <= 900.0:
        raise CandidateSandboxError("candidate_sandbox_timeout_out_of_range")
    command = ["/usr/bin/python3", *map(str, argv)]
    # Pipes let an untrusted import make the host's communicate() buffer grow
    # without bound. Anonymous host-owned files keep output outside the
    # candidate namespace; RLIMIT_FSIZE bounds writes and the parent only reads
    # a capped tail (which contains trusted completion receipts).
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
        mode="w+b"
    ) as stderr_file:
        try:
            managed = launch_isolated_worker(
                artifact_root,
                command,
                environment=environment,
                readonly_inputs=readonly_inputs,
                output_files=(
                    {"trusted_completion": _trusted_completion[0]}
                    if _trusted_completion is not None
                    else None
                ),
                trusted_control_fds=(
                    (_trusted_completion[2],)
                    if _trusted_completion is not None
                    else ()
                ),
                stdin=(
                    subprocess.PIPE
                    if input_text is not None
                    else subprocess.DEVNULL
                ),
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
                host_process_owner="candidate-verification",
            )
        except ManagedExecutorError as exc:
            if _trusted_completion is not None:
                for descriptor in (_trusted_completion[1], _trusted_completion[2]):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            raise CandidateSandboxError(
                "candidate_sandbox_isolation_unavailable:"
                f"{type(exc).__name__}:{exc}"
            ) from exc

        process = managed.process
        if _trusted_completion is not None:
            try:
                os.close(_trusted_completion[2])
            except OSError:
                pass
        try:
            process.communicate(
                input=(
                    input_text.encode("utf-8")
                    if input_text is not None
                    else None
                ),
                timeout=timeout_value,
            )
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise CandidateSandboxTimeout(
                f"candidate_sandbox_timeout_after_{timeout_value:g}s"
            ) from exc
        except BaseException:
            _terminate_process_group(process)
            raise
        completion_verified = False
        if _trusted_completion is not None:
            completion_path = Path(_trusted_completion[0])
            token_chunks: list[bytes] = []
            try:
                while sum(map(len, token_chunks)) <= 256:
                    chunk = os.read(_trusted_completion[1], 257)
                    if not chunk:
                        break
                    token_chunks.append(chunk)
            finally:
                os.close(_trusted_completion[1])
            token_line = b"".join(token_chunks)
            try:
                token = token_line.decode("ascii").strip()
            except UnicodeDecodeError:
                token = ""
            expected = _completion_payload(token) if len(token) == 64 else b""
            try:
                observed = completion_path.read_bytes()
            except OSError:
                observed = b""
            completion_verified = (
                int(process.returncode) == 0
                and bool(expected)
                and observed == expected
            )
        return CandidateSandboxResult(
            returncode=int(process.returncode),
            stdout=_read_capped(stdout_file),
            stderr=_read_capped(stderr_file),
            isolation=managed.isolation,
            trusted_completion=completion_verified,
        )


def run_candidate_probe(
    artifact_root: str | Path,
    source: str,
    *,
    args: Sequence[str] = (),
    timeout: float = 20.0,
    input_text: str | None = None,
    environment: Mapping[str, object] | None = None,
    readonly_inputs: Mapping[str, str | Path] | None = None,
) -> CandidateSandboxResult:
    """Run a trusted ephemeral harness with the candidate mounted at /work."""

    if not isinstance(source, str) or not source.strip():
        raise CandidateSandboxError("candidate_sandbox_probe_source_invalid")
    with tempfile.TemporaryDirectory(prefix="pok_candidate_probe_") as raw_root:
        harness_root = Path(raw_root)
        # The sandbox changes to uid/gid 65534.  The bind source is opened by
        # bwrap first, but the child still needs traverse/read permission.
        harness_root.chmod(0o755)
        token_read_fd, token_write_fd = os.pipe()
        os.set_inheritable(token_read_fd, False)
        os.set_inheritable(token_write_fd, False)
        wrapped_source = _trusted_probe_wrapper(source, token_write_fd)
        probe = harness_root / "probe.py"
        descriptor = os.open(
            probe,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_CLOEXEC", 0)),
            0o444,
        )
        try:
            payload = wrapped_source.encode("utf-8")
            while payload:
                written = os.write(descriptor, payload)
                if written <= 0:
                    raise CandidateSandboxError("candidate_sandbox_probe_write_failed")
                payload = payload[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        named_inputs = dict(readonly_inputs or {})
        if "harness" in named_inputs:
            raise CandidateSandboxError(
                "candidate_sandbox_probe_reserved_readonly_input"
            )
        named_inputs["harness"] = harness_root
        completion_path = harness_root / "trusted-completion.receipt"
        try:
            return run_candidate_python(
                artifact_root,
                ["-I", "-B", "/inputs/harness/probe.py", *map(str, args)],
                timeout=timeout,
                input_text=input_text,
                environment=environment,
                readonly_inputs=named_inputs,
                _trusted_completion=(
                    completion_path,
                    token_read_fd,
                    token_write_fd,
                ),
            )
        except BaseException:
            for descriptor in (token_read_fd, token_write_fd):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise


def isolation_receipt(identity: IsolationIdentity) -> dict[str, object]:
    """Return compact, serializable evidence for an authoritative gate."""

    return {
        "schema": "candidate-execution-sandbox-v1",
        "policy_sha256": identity.policy_sha256,
        "bpf_sha256": identity.bpf_sha256,
        "bpf_size": identity.bpf_size,
        "namespaces": list(identity.namespaces),
        "network": identity.network,
        "readonly_inputs": identity.readonly_inputs,
        "writable_outputs": identity.writable_outputs,
        "resource_limits": [list(item) for item in identity.resource_limits],
    }
