from pathlib import Path
import socket

import pytest

from bot_artifact import hash_path
from managed_bot_executor import EndpointLease, EndpointLeaseError
from national_arena import sandbox as arena_sandbox


def _listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener


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


def test_arena_launch_uses_central_managed_executor(tmp_path):
    source = _bot_source(tmp_path)
    sealed = arena_sandbox.seal_bot_artifact(
        source,
        tmp_path / "sealed" / "bot",
        expected_hash=hash_path(source),
    )
    capability = arena_sandbox.require_managed_sandbox(probe=False)

    listener = _listener()
    port = int(listener.getsockname()[1])
    try:
        with EndpointLease.connect("127.0.0.1", port, timeout=2.0) as endpoint:
            accepted, _peer = listener.accept()
            managed = arena_sandbox.launch_sandboxed_bot(
                sealed,
                capability,
                endpoint,
                name="BoundTop",
                seat="upper",
                session_id="arena_20260711_cafebabe",
                action_delay=0.30,
                hard_deadline=55.0,
                refinement_budget=54.0,
                baseline_target=0.25,
            )
        stdout, stderr = managed.process.communicate(timeout=10)
        accepted.close()
    finally:
        listener.close()

    assert managed.process.returncode == 0, (stdout, stderr)
    assert managed.isolation.network == "isolated-netns-inherited-exact-peer-only"
    assert managed.isolation.resource_limits


def test_arena_sandbox_missing_or_unusable_bwrap_fails_closed(monkeypatch):
    with pytest.raises(
        arena_sandbox.ArenaSandboxUnavailable,
        match="no fallback",
    ):
        arena_sandbox.require_managed_sandbox(
            environment={"POK_ARENA_BWRAP": "/definitely/missing/bwrap"}
        )

    monkeypatch.setattr(
        arena_sandbox,
        "probe_managed_executor",
        lambda _runtime: {"ok": False, "issues": ["namespace unavailable"]},
    )
    with pytest.raises(
        arena_sandbox.ArenaSandboxUnavailable,
        match="probe_failed",
    ):
        arena_sandbox.require_managed_sandbox()


def test_arena_sandbox_rejects_non_loopback_endpoint():
    with pytest.raises(EndpointLeaseError, match="loopback"):
        EndpointLease.connect("0.0.0.0", 12345, timeout=0.1)


def test_arena_never_seals_or_launches_quarantined_raw_entry(
    tmp_path, monkeypatch
):
    repository_root = Path(__file__).resolve().parents[2]
    quarantined = repository_root / "bots" / "national_v142"
    with pytest.raises(
        arena_sandbox.ArenaSandboxError,
        match="protocol_quarantined_native_entry_forbidden",
    ):
        arena_sandbox.seal_bot_artifact(
            quarantined,
            tmp_path / "sealed" / "v142",
            expected_hash=hash_path(quarantined),
        )

    ordinary_root = tmp_path / "ordinary"
    ordinary_root.mkdir()
    source = _bot_source(ordinary_root)
    sealed = arena_sandbox.seal_bot_artifact(
        source,
        tmp_path / "sealed" / "ordinary",
        expected_hash=hash_path(source),
    )
    monkeypatch.setattr(
        arena_sandbox,
        "quarantined_native_entry_sources",
        lambda _path: ("national_v142",),
    )
    with pytest.raises(
        arena_sandbox.ArenaSandboxError,
        match="protocol_quarantined_native_entry_forbidden",
    ):
        arena_sandbox.launch_sandboxed_bot(
            sealed,
            object(),
            object(),
            name="v142-copy",
            seat="upper",
            session_id="arena_20260711_deadbeef",
            action_delay=0.3,
            hard_deadline=55.0,
            refinement_budget=54.0,
            baseline_target=0.25,
        )
