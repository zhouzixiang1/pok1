from pathlib import Path
import socket

from bot_artifact import hash_path
from official_bot_sandbox import build_sandboxed_bot_command, seal_bot_artifact
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


def test_sandbox_command_exposes_only_bot_and_single_log(tmp_path):
    source = _bot(tmp_path / "national_v1")
    sealed = seal_bot_artifact(
        source,
        tmp_path / "sealed" / "candidate",
        expected_hash=hash_path(source),
    )
    log_path = tmp_path / "evidence" / "botA.log"

    peer, inherited = socket.socketpair()
    try:
        command, environment = build_sandboxed_bot_command(
            sealed,
            host="127.0.0.1",
            port=10001,
            name="candidate",
            seat="upper",
            log_path=log_path,
            supports_log=True,
            preconnected_fd=inherited.fileno(),
        )
    finally:
        peer.close()
        inherited.close()

    joined = "\0".join(command)
    assert "--unshare-all" in command
    assert "--share-net" not in command
    assert "POK_PRECONNECTED_SOCKET_FD" in command
    assert "--clearenv" in command
    assert "-I" in command and "-B" in command
    assert str(sealed.root) in command
    assert str(log_path) in command
    assert str(source) not in joined
    assert str(Path.home()) not in joined
    assert "PYTHONPATH" not in joined
    assert set(environment) <= {"PATH", "POK_OFFICIAL_JOB_PROCESS_GROUP"}


def test_execution_profile_is_tracked_and_requires_sandbox():
    profile = load_execution_profile()
    identity = execution_profile_identity()

    assert profile["official_exe"]["sha256"] == (
        "9d01b443d4920a7e06a487d87ea1b050ea2ca5359023602f98c3c236c734e81a"
    )
    assert profile["sandbox"]["required"] is True
    assert profile["sandbox"]["network"] == "isolated-netns-preconnected-socket-fd"
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
