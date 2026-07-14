from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from bot_artifact import hash_path
from bot_namespace import STRICT_ARTIFACT_FILES, strict_artifact_layout_errors
from managed_bot_executor import EndpointLease, EndpointLeaseError
from national_arena import sandbox as arena_sandbox


def _listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener


def _bot_source(tmp_path: Path) -> Path:
    source = tmp_path / "national_v143"
    source.mkdir()
    payloads = {
        "national_bot.py": "print('strict arena fixture')\n",
        "precompute.py": "FACT = 1\n",
        "policy.py": (
            "def get_baseline_decision(context):\n"
            "    return {'kind': 'pass'}\n"
            "def iter_decisions(context, baseline, deadline):\n"
            "    return iter(())\n"
        ),
        "national_runtime_manifest.json": "{}\n",
        "policy_epoch_receipt.json": "{}\n",
    }
    assert frozenset(payloads) == STRICT_ARTIFACT_FILES
    for relative, payload in payloads.items():
        (source / relative).write_text(payload, encoding="utf-8")
    assert strict_artifact_layout_errors(source) == []
    return source


def _allow_strict_fixture(monkeypatch):
    monkeypatch.setattr(
        arena_sandbox,
        "resolve_national_bot_spec",
        lambda *_args, **_kwargs: SimpleNamespace(eligible=True, issues=()),
    )
    monkeypatch.setattr(
        arena_sandbox,
        "current_system_native_runtime_errors",
        lambda _path: [],
    )
    monkeypatch.setattr(arena_sandbox, "runtime_manifest_errors", lambda _path: [])


def test_arena_seal_is_content_bound_and_read_only(tmp_path, monkeypatch):
    _allow_strict_fixture(monkeypatch)
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

    (source / "policy.py").write_text("# changed policy\n", encoding="utf-8")
    with pytest.raises(arena_sandbox.ArenaSandboxError, match="identity_mismatch"):
        arena_sandbox.seal_bot_artifact(
            source,
            tmp_path / "session" / "bottom" / "bot",
            expected_hash=expected_hash,
        )


def test_arena_launch_uses_central_managed_executor(tmp_path, monkeypatch):
    _allow_strict_fixture(monkeypatch)
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


def test_arena_seal_rejects_ineligible_strict_artifact(tmp_path, monkeypatch):
    source = _bot_source(tmp_path)
    monkeypatch.setattr(
        arena_sandbox,
        "resolve_national_bot_spec",
        lambda *_args, **_kwargs: SimpleNamespace(
            eligible=False,
            issues=("full_certificate_missing",),
        ),
    )

    with pytest.raises(
        arena_sandbox.ArenaSandboxError,
        match="arena_requires_strict_full_certified_policy_artifact",
    ):
        arena_sandbox.seal_bot_artifact(
            source,
            tmp_path / "sealed" / "ineligible",
            expected_hash=hash_path(source),
        )


def test_arena_seal_rejects_noncurrent_system_runtime(tmp_path, monkeypatch):
    source = _bot_source(tmp_path)
    monkeypatch.setattr(
        arena_sandbox,
        "resolve_national_bot_spec",
        lambda *_args, **_kwargs: SimpleNamespace(eligible=True, issues=()),
    )

    with pytest.raises(
        arena_sandbox.ArenaSandboxError,
        match="non_system_owned_native_runtime_forbidden",
    ):
        arena_sandbox.seal_bot_artifact(
            source,
            tmp_path / "sealed" / "runtime-drift",
            expected_hash=hash_path(source),
        )


def test_arena_launch_revalidates_sealed_system_runtime(tmp_path, monkeypatch):
    _allow_strict_fixture(monkeypatch)
    source = _bot_source(tmp_path)
    sealed = arena_sandbox.seal_bot_artifact(
        source,
        tmp_path / "sealed" / "bot",
        expected_hash=hash_path(source),
    )
    monkeypatch.setattr(
        arena_sandbox,
        "current_system_native_runtime_errors",
        lambda _path: ["system_owned_native_runtime_identity_mismatch"],
    )

    with pytest.raises(
        arena_sandbox.ArenaSandboxError,
        match="arena_sealed_policy_runtime_invalid",
    ):
        arena_sandbox.launch_sandboxed_bot(
            sealed,
            object(),
            object(),
            name="runtime-drift",
            seat="upper",
            session_id="arena_20260714_deadbeef",
            action_delay=0.3,
            hard_deadline=55.0,
            refinement_budget=54.0,
            baseline_target=0.25,
        )
