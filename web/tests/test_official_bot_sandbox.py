from pathlib import Path
import socket

import pytest

from bot_artifact import hash_path
from managed_bot_executor import EndpointLease
import official_bot_sandbox
from official_bot_sandbox import (
    SealedBotArtifact,
    launch_sandboxed_bot,
    seal_bot_artifact,
)
from official_execution_profile import execution_profile_identity, load_execution_profile
from official_platform_harness import OfficialPlatformConfig, check_environment


def _bot(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "national_bot.py").write_text(
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--host')\n"
        "parser.add_argument('--port')\n"
        "parser.add_argument('--name')\n"
        "parser.add_argument('--seat')\n"
        "parser.add_argument('--log')\n"
        "parser.parse_args()\n",
        encoding="utf-8",
    )
    (path / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
    return path


def test_sealed_bot_contains_only_content_bound_artifact(tmp_path):
    source = _bot(tmp_path / "national_v1")
    (source / ".completed").write_text("runtime-only\n", encoding="utf-8")
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "strategy.pyc").write_bytes(b"runtime-cache")
    expected_hash = hash_path(source)

    sealed = seal_bot_artifact(
        source,
        tmp_path / "suite" / "sealed" / "candidate",
        expected_hash=expected_hash,
    )

    assert sealed.artifact_hash == expected_hash
    assert hash_path(sealed.root) == expected_hash
    assert not (sealed.root / ".completed").exists()
    assert not (sealed.root / "__pycache__").exists()
    assert (sealed.root.stat().st_mode & 0o222) == 0
    assert (sealed.root / "national_bot.py").stat().st_mode & 0o222 == 0


def test_formal_sandbox_launch_uses_central_executor_and_single_log(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "official_bot_sandbox.current_system_native_runtime_errors",
        lambda _path: [],
    )
    source = _bot(tmp_path / "national_v1")
    sealed = seal_bot_artifact(
        source,
        tmp_path / "sealed" / "candidate",
        expected_hash=hash_path(source),
    )
    log_path = tmp_path / "evidence" / "botA.log"

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    try:
        with EndpointLease.connect("127.0.0.1", port, timeout=2.0) as endpoint:
            accepted, _peer = listener.accept()
            managed = launch_sandboxed_bot(
                sealed,
                endpoint,
                name="candidate",
                seat="upper",
                log_path=log_path,
                supports_log=True,
            )
        stdout, stderr = managed.process.communicate(timeout=10)
        accepted.close()
    finally:
        listener.close()

    assert managed.process.returncode == 0, (stdout, stderr)
    assert managed.isolation.network == "isolated-netns-inherited-exact-peer-only"
    assert managed.isolation.nested_userns == "disabled-and-asserted"
    assert managed.isolation.capabilities == "drop-all"
    assert managed.isolation.bpf_size > 0
    assert log_path.is_file()


def test_execution_profile_is_tracked_and_requires_sandbox():
    profile = load_execution_profile()
    identity = execution_profile_identity()

    assert profile["official_exe"]["sha256"] == (
        "9d01b443d4920a7e06a487d87ea1b050ea2ca5359023602f98c3c236c734e81a"
    )
    assert profile["sandbox"]["required"] is True
    assert profile["sandbox"]["network"] == (
        "isolated-netns-loopback-exact-peer-inherited-stream-only"
    )
    assert profile["sandbox"]["authority"] == "web/core/managed_bot_executor.py"
    assert profile["managed_executor"]["contract"]["host_root_mounted"] is False
    assert profile["sandbox"]["python_flags"] == ["-I", "-B"]
    assert len(identity["profile_sha256"]) == 64
    assert len(identity["profile_digest"]) == 64


def test_formal_environment_fails_closed_when_sandbox_probe_fails(tmp_path, monkeypatch):
    exe = tmp_path / "platform.exe"
    exe.write_bytes(b"fake")
    wineprefix = tmp_path / "wine"
    wineprefix.mkdir()
    config = OfficialPlatformConfig(
        exe_path=exe,
        wineprefix=wineprefix,
        results_dir=tmp_path / "results",
        lock_path=tmp_path / "lock",
    )
    monkeypatch.setattr(
        "official_platform_harness.validate_execution_profile",
        lambda *_args, **_kwargs: {
            "ok": False,
            "issues": ["official_sandbox_probe_failed"],
        },
    )

    report = check_environment(config, require_formal_sandbox=True)

    assert report["ok"] is False
    assert "official_sandbox_probe_failed" in report["issues"]


def test_direct_official_launch_rejects_archived_raw_entry(tmp_path, monkeypatch):
    archived = _bot(tmp_path / "archived_runtime")
    artifact_hash = hash_path(archived)
    artifact = SealedBotArtifact(
        source=archived,
        root=archived,
        entry_relative="national_bot.py",
        artifact_hash=artifact_hash,
        manifest_digest="a" * 64,
    )
    monkeypatch.setattr(
        "official_bot_sandbox.current_system_native_runtime_errors",
        lambda _path: ["system_owned_native_runtime_identity_mismatch"],
    )

    with pytest.raises(
        RuntimeError,
        match="non_system_owned_native_runtime_forbidden",
    ):
        launch_sandboxed_bot(
            artifact,
            object(),
            name="archived",
            seat="lower",
            log_path=None,
            supports_log=False,
        )


def test_formal_sandbox_exposes_no_archived_runtime_bearer_waiver():
    assert not hasattr(
        official_bot_sandbox,
        "OfficialBootstrapLaunchAuthorization",
    )
    assert "quarantine_authorization" not in (
        __import__("inspect").signature(launch_sandboxed_bot).parameters
    )


def test_direct_official_launch_rejects_noncurrent_runtime_without_lineage(
    tmp_path, monkeypatch
):
    source = _bot(tmp_path / "renamed_legacy")
    artifact_hash = hash_path(source)
    artifact = SealedBotArtifact(
        source=source,
        root=source,
        entry_relative="national_bot.py",
        artifact_hash=artifact_hash,
        manifest_digest="a" * 64,
    )
    with pytest.raises(
        RuntimeError,
        match="non_system_owned_native_runtime_forbidden",
    ):
        launch_sandboxed_bot(
            artifact,
            object(),
            name="renamed-legacy",
            seat="lower",
            log_path=None,
            supports_log=False,
        )
