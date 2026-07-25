import asyncio
import importlib.util
import inspect
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from bot_namespace import (
    NATIONAL_RUNTIME_MANIFEST,
    POLICY_EPOCH_RECEIPT,
    build_policy_epoch_receipt,
    build_runtime_manifest,
    canonical_identity_document_bytes,
)
from conftest import STRICT_TARGET_V, strict_bot_name
from national_native import ensure_native_entry


ROOT = Path(__file__).resolve().parents[2]


def _load_cli():
    name = "official_wire_probe_cli_boundary"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "scripts" / "official_wire_probe.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _strict_bot(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    return {'kind': 'pass'}\n\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    return ()\n",
        encoding="utf-8",
    )
    ensure_native_entry(root, overwrite=True)
    manifest = build_runtime_manifest(root)
    (root / NATIONAL_RUNTIME_MANIFEST).write_bytes(
        canonical_identity_document_bytes(manifest),
    )
    receipt = build_policy_epoch_receipt(
        root,
        STRICT_TARGET_V,
        parent_versions=(),
    )
    (root / POLICY_EPOCH_RECEIPT).write_bytes(
        canonical_identity_document_bytes(receipt),
    )
    return root


def test_wire_probe_is_short_diagnostic_and_rejects_unbound_hand_70(capsys):
    module = _load_cli()
    required = ["--candidate", "/candidate", "--opponent", "/opponent"]

    assert module.parse_args(required).target_hands == 1
    assert module.parse_args([*required, "--target-hands", "69"]).target_hands == 69
    with pytest.raises(SystemExit):
        module.parse_args([*required, "--target-hands", "70"])
    assert "requires scripts/official_certify.py full" in capsys.readouterr().err


def test_runtime_scripts_import_with_script_directory_before_core():
    env = dict(os.environ)
    env["PYTHONPATH"] = "web/core:."
    for script in (
        "scripts/official_wire_probe.py",
        "scripts/abandon_parked_bootstrap_contract_change.py",
    ):
        completed = subprocess.run(
            [sys.executable, script, "--help"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr


def test_wire_probe_rejects_arbitrary_script_and_symlink_paths(tmp_path):
    module = _load_cli()
    script = tmp_path / "arbitrary.py"
    script.write_text("raise SystemExit('host execution')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="arbitrary script paths are forbidden"):
        module._strict_bot_directory(script)

    bot = _strict_bot(tmp_path / strict_bot_name())
    alias = tmp_path / "candidate-alias"
    alias.symlink_to(bot, target_is_directory=True)
    with pytest.raises(ValueError, match="strict_bot_directory"):
        module._strict_bot_directory(alias)


def test_wire_probe_invalid_source_is_recorded_before_platform_launch(
    tmp_path,
    monkeypatch,
):
    module = _load_cli()
    arbitrary = tmp_path / "arbitrary.py"
    arbitrary.write_text("raise SystemExit('host execution')\n", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "check_environment",
        lambda *_args, **_kwargs: {"ok": True, "issues": [], "warnings": []},
    )

    receipt = asyncio.run(
        module._run_probe_round(
            module.parse_args(
                [
                    "--candidate",
                    str(arbitrary),
                    "--opponent",
                    str(arbitrary),
                    "--results-dir",
                    str(tmp_path / "results"),
                ]
            )
        )
    )

    assert receipt["passed"] is False
    assert receipt["source_validation"]["validation_digest"] == ""
    assert "arbitrary script paths are forbidden" in receipt["issues"][0]
    assert Path(receipt["artifacts"]["round_dir"], "receipt.json").is_file()


def test_wire_probe_rejects_pyc_before_endpoint_or_output_consumption(tmp_path):
    module = _load_cli()
    bot = _strict_bot(tmp_path / "bots" / strict_bot_name())
    cache = bot / "__pycache__"
    cache.mkdir()
    (cache / "policy.cpython-test.pyc").write_bytes(b"unchecked-cache")
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"

    with pytest.raises(
        RuntimeError,
        match="artifact_execution_cache_directory_forbidden:__pycache__",
    ):
        module._launch_managed_probe_bot(
            bot_path=bot,
            name="Candidate",
            seat="upper",
            host="127.0.0.1",
            port=1,
            log_path=tmp_path / "bot.log",
            sealed_root=tmp_path / "sealed",
            stdout_path=stdout,
            stderr_path=stderr,
        )
    assert not stdout.exists()
    assert not stderr.exists()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("task_context", "artifact_working_control_directory_forbidden:.task_context"),
        ("extra", "artifact_extra_file_forbidden:sitecustomize.py"),
        ("wrong_runtime", "system_owned_native_runtime_identity_mismatch"),
        ("fake_manifest", "runtime_manifest_keys_mismatch"),
    ),
)
def test_wire_probe_canonical_candidate_validation_rejects_pollution_and_forgery(
    tmp_path,
    mutation,
    expected,
):
    module = _load_cli()
    bot = _strict_bot(tmp_path / mutation / "bots" / strict_bot_name())
    if mutation == "task_context":
        control = bot / ".task_context"
        control.mkdir()
        (control / "worker.md").write_text("work-only\n", encoding="utf-8")
    elif mutation == "extra":
        (bot / "sitecustomize.py").write_text("PWNED = True\n", encoding="utf-8")
    elif mutation == "wrong_runtime":
        with (bot / "national_bot.py").open("a", encoding="utf-8") as stream:
            stream.write("\n# candidate-owned runtime drift\n")
    elif mutation == "fake_manifest":
        (bot / NATIONAL_RUNTIME_MANIFEST).write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=expected):
        module._validate_strict_candidate(bot)


def test_wire_probe_strict_candidate_uses_managed_formal_launch_not_host_popen(
    tmp_path,
    monkeypatch,
):
    module = _load_cli()
    source = inspect.getsource(module._launch_managed_probe_bot)
    assert "launch_sandboxed_bot(" in source
    assert "EndpointLease.connect(" in source
    assert "_popen(" not in source
    assert "subprocess.Popen(" not in source

    def forbidden_host_popen(*_args, **_kwargs):
        raise AssertionError("bot must not use the host _popen path")

    monkeypatch.setattr(module, "_popen", forbidden_host_popen)
    bot = _strict_bot(tmp_path / "bots" / strict_bot_name())
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5.0)
    process = None
    accepted = None
    try:
        process, receipt = module._launch_managed_probe_bot(
            bot_path=bot,
            name="ProbeCandidate",
            seat="upper",
            host="127.0.0.1",
            port=int(listener.getsockname()[1]),
            log_path=tmp_path / "bot.log",
            sealed_root=tmp_path / "sealed",
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr.log",
        )
        accepted, _peer = listener.accept()
        accepted.settimeout(5.0)
        accepted.sendall(b"name")
        assert accepted.recv(256) == b"ProbeCandidate"
    finally:
        if accepted is not None:
            accepted.close()
        if process is not None:
            module._terminate_process(process)
            module._close_process_files(process)
        listener.close()

    assert receipt["mode"] == "central-managed-sealed-source-projection"
    assert receipt["endpoint_lease"] == {"consumed": True, "closed": True}
    assert receipt["isolation"]["network"] == (
        "isolated-netns-inherited-exact-peer-only"
    )
    assert len(receipt["artifact_hash"]) == 64
    assert receipt["source_validation"]["artifact_hash"] == receipt["artifact_hash"]
    assert receipt["source_validation"]["issues"] == []
    assert len(receipt["source_validation"]["validation_digest"]) == 64
