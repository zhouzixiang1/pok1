from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from bot_artifact import hash_path
from national_arena import sandbox as arena_sandbox


@pytest.fixture
def preconnected_fd():
    peer, inherited = socket.socketpair()
    try:
        yield inherited.fileno()
    finally:
        peer.close()
        inherited.close()


def _bot_source(tmp_path: Path) -> Path:
    source = tmp_path / "national_v1"
    source.mkdir()
    (source / "national_bot.py").write_text(
        "from helper import value\nprint(value)\n",
        encoding="utf-8",
    )
    (source / "helper.py").write_text("value = 7\n", encoding="utf-8")
    return source


def test_arena_seal_is_content_bound_and_read_only(tmp_path):
    source = _bot_source(tmp_path)
    expected_hash = hash_path(source)

    sealed = arena_sandbox.seal_bot_artifact(
        source,
        tmp_path / "session" / "top" / "bot",
        expected_hash=expected_hash,
    )

    assert sealed.artifact_hash == expected_hash
    assert hash_path(sealed.root) == expected_hash
    assert (sealed.root / "national_bot.py").stat().st_mode & 0o222 == 0
    assert sealed.root.stat().st_mode & 0o222 == 0

    (source / "helper.py").write_text("value = 8\n", encoding="utf-8")
    with pytest.raises(arena_sandbox.ArenaSandboxError, match="identity_mismatch"):
        arena_sandbox.seal_bot_artifact(
            source,
            tmp_path / "session" / "bottom" / "bot",
            expected_hash=expected_hash,
        )


def test_arena_sandbox_plan_exposes_no_writable_host_bind(tmp_path, preconnected_fd):
    source = _bot_source(tmp_path)
    sealed = arena_sandbox.seal_bot_artifact(
        source,
        tmp_path / "sealed" / "bot",
        expected_hash=hash_path(source),
    )
    capability = arena_sandbox.require_managed_sandbox(probe=False)

    launch = arena_sandbox.build_sandboxed_bot_launch(
        sealed,
        capability,
        host="127.0.0.1",
        port=43210,
        name="BoundTop",
        seat="upper",
        session_id="arena_20260711_cafebabe",
        action_delay=0.30,
        hard_deadline=55.0,
        refinement_budget=54.0,
        baseline_target=0.25,
        preconnected_fd=preconnected_fd,
    )

    command = list(launch.command)
    assert "--unshare-all" in command
    assert "--share-net" not in command
    assert "POK_PRECONNECTED_SOCKET_FD" in command
    assert launch.pass_fds == (preconnected_fd,)
    assert "--bind" not in command
    assert command[command.index("--ro-bind", command.index("--tmpfs")) + 1] == str(
        sealed.root
    )
    assert command[-8:] == [
        "--host", "127.0.0.1",
        "--port", "43210",
        "--name", "BoundTop",
        "--seat", "upper",
    ]
    protected_tokens = (
        str(arena_sandbox.ROOT / "bots"),
        str(arena_sandbox.ROOT / "web" / "core" / "results"),
        str(arena_sandbox.ROOT / "official_certificates"),
    )
    assert not any(token in command for token in protected_tokens)
    assert launch.environment == {
        "PATH": arena_sandbox.os.defpath,
        "POK_ARENA_SESSION_ID": "arena_20260711_cafebabe",
    }


def test_arena_sandbox_missing_or_unusable_bwrap_fails_closed(monkeypatch):
    monkeypatch.setattr(arena_sandbox.shutil, "which", lambda _name: None)
    with pytest.raises(
        arena_sandbox.ArenaSandboxUnavailable,
        match="no fallback",
    ):
        arena_sandbox.require_managed_sandbox()

    monkeypatch.setattr(
        arena_sandbox.shutil,
        "which",
        lambda _name: "/usr/bin/bwrap",
    )
    monkeypatch.setattr(
        arena_sandbox.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stderr="Creating new namespace failed",
        ),
    )
    with pytest.raises(
        arena_sandbox.ArenaSandboxUnavailable,
        match="namespace_unavailable",
    ):
        arena_sandbox.require_managed_sandbox()


def test_arena_sandbox_rejects_non_loopback_endpoint(tmp_path, preconnected_fd):
    source = _bot_source(tmp_path)
    sealed = arena_sandbox.seal_bot_artifact(
        source,
        tmp_path / "sealed" / "bot",
        expected_hash=hash_path(source),
    )
    capability = arena_sandbox.require_managed_sandbox(probe=False)

    with pytest.raises(arena_sandbox.ArenaSandboxError, match="must_be_loopback"):
        arena_sandbox.build_sandboxed_bot_launch(
            sealed,
            capability,
            host="0.0.0.0",
            port=12345,
            name="Bad",
            seat="upper",
            session_id="arena_20260711_deadbeef",
            action_delay=0.30,
            hard_deadline=55.0,
            refinement_budget=54.0,
            baseline_target=0.25,
            preconnected_fd=preconnected_fd,
        )
