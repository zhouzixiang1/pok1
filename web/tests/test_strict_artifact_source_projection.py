import json
import py_compile
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import managed_bot_executor as executor
from bot_artifact import hash_path, publication_shape_errors
from bot_namespace import STRICT_ARTIFACT_FILES, strict_artifact_layout_errors


def _listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    listener.settimeout(5.0)
    return listener


def _receive_line(sock: socket.socket) -> bytes:
    payload = bytearray()
    sock.settimeout(8.0)
    while b"\n" not in payload:
        chunk = sock.recv(4096)
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload).partition(b"\n")[0]


def _strict_source_bot(root: Path) -> None:
    root.mkdir()
    (root / "national_bot.py").write_text(
        "import argparse,json,os,socket,sys,policy,precompute\n"
        "p=argparse.ArgumentParser();p.add_argument('--host');"
        "p.add_argument('--port',type=int);p.add_argument('--name');a=p.parse_args()\n"
        "s=socket.create_connection((a.host,a.port));"
        "s.sendall((json.dumps({"
        "'files':sorted(os.listdir('/bot')),'policy':policy.VALUE,"
        "'precompute':precompute.VALUE,"
        "'sitecustomize_loaded':'sitecustomize' in sys.modules,"
        "'site_marker':os.environ.get('PWNED_SITE'),"
        "'isolated':sys.flags.isolated,"
        "'dont_write_bytecode':sys.dont_write_bytecode})+'\\n').encode());s.close()\n",
        encoding="utf-8",
    )
    (root / "policy.py").write_text("VALUE = 'bound-policy-source'\n", encoding="utf-8")
    (root / "precompute.py").write_text(
        "VALUE = 'bound-precompute-source'\n", encoding="utf-8"
    )
    (root / "national_runtime_manifest.json").write_text("{}\n", encoding="utf-8")
    (root / "policy_epoch_receipt.json").write_text("{}\n", encoding="utf-8")


def _inject_unchecked_bytecode(root: Path) -> None:
    cache = root / "__pycache__"
    cache.mkdir(exist_ok=True)
    for module in ("policy", "precompute"):
        malicious = root / f"malicious_{module}.py"
        malicious.write_text(
            f"VALUE = 'UNBOUND-{module.upper()}-PYC'\n",
            encoding="utf-8",
        )
        py_compile.compile(
            str(malicious),
            cfile=str(cache / f"{module}.{sys.implementation.cache_tag}.pyc"),
            doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )
        malicious.unlink()


def _prove_fixture_overrides_sources_without_projection(root: Path) -> dict:
    command = (
        "import json,policy,precompute;"
        "print(json.dumps({'policy':policy.VALUE,'precompute':precompute.VALUE}))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", f"import sys;sys.path.insert(0,{str(root)!r});{command}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def test_strict_layout_and_publication_reject_all_cache_and_work_control_entries(
    tmp_path,
):
    root = tmp_path / "national_v143"
    _strict_source_bot(root)
    assert strict_artifact_layout_errors(root) == []

    task_context = root / ".task_context"
    task_context.mkdir()
    (task_context / "worker.md").write_text("compiler-owned", encoding="utf-8")
    assert strict_artifact_layout_errors(
        root, allow_working_task_context=True
    ) == []
    assert strict_artifact_layout_errors(root) == [
        "artifact_working_control_directory_forbidden:.task_context"
    ]
    (task_context / "worker.md").unlink()
    task_context.rmdir()

    _inject_unchecked_bytecode(root)
    (root / "rogue.pyc").write_bytes(b"not even valid bytecode")
    errors = strict_artifact_layout_errors(root)
    assert "artifact_execution_cache_directory_forbidden:__pycache__" in errors
    assert "artifact_execution_cache_file_forbidden:rogue.pyc" in errors
    publication = publication_shape_errors(root, repo_root=tmp_path)
    assert "artifact_execution_cache_directory_forbidden:__pycache__" in publication
    assert "artifact_execution_cache_file_forbidden:rogue.pyc" in publication


def test_preexisting_unchecked_policy_and_precompute_pyc_never_reach_managed_launch(
    tmp_path,
):
    root = tmp_path / "national_v143"
    _strict_source_bot(root)
    expected_hash = hash_path(root)
    _inject_unchecked_bytecode(root)
    assert _prove_fixture_overrides_sources_without_projection(root) == {
        "policy": "UNBOUND-POLICY-PYC",
        "precompute": "UNBOUND-PRECOMPUTE-PYC",
    }

    listener = _listener()
    lease = executor.EndpointLease.connect(
        "127.0.0.1", int(listener.getsockname()[1]), timeout=2.0
    )
    accepted, _peer = listener.accept()
    try:
        with pytest.raises(
            executor.ManagedExecutorError,
            match="managed bot unbound directory is forbidden: __pycache__",
        ):
            executor.launch_managed_bot(
                root,
                lease,
                name="cache-rejected",
                expected_artifact_hash=expected_hash,
                required_artifact_files=tuple(sorted(STRICT_ARTIFACT_FILES)),
            )
        assert lease.consumed is False
    finally:
        lease.close()
        accepted.close()
        listener.close()


def test_post_snapshot_cache_and_sitecustomize_injection_cannot_change_executed_bytes(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "national_v143"
    _strict_source_bot(root)
    expected_hash = hash_path(root)
    original_snapshot = executor.snapshot_managed_bot_sources
    injected = False

    def snapshot_then_inject(*args, **kwargs):
        nonlocal injected
        snapshot = original_snapshot(*args, **kwargs)
        if not injected:
            injected = True
            _inject_unchecked_bytecode(root)
            (root / "sitecustomize.py").write_text(
                "import os\nos.environ['PWNED_SITE'] = 'yes'\n",
                encoding="utf-8",
            )
        return snapshot

    monkeypatch.setattr(executor, "snapshot_managed_bot_sources", snapshot_then_inject)
    listener = _listener()
    lease = executor.EndpointLease.connect(
        "127.0.0.1", int(listener.getsockname()[1]), timeout=2.0
    )
    accepted, _peer = listener.accept()
    try:
        launched = executor.launch_managed_bot(
            root,
            lease,
            name="sealed-projection",
            expected_artifact_hash=expected_hash,
            required_artifact_files=tuple(sorted(STRICT_ARTIFACT_FILES)),
        )
        report = json.loads(_receive_line(accepted).decode("utf-8"))
        stdout, stderr = launched.process.communicate(timeout=10.0)
        assert launched.process.returncode == 0, (stdout, stderr)
    finally:
        lease.close()
        accepted.close()
        listener.close()

    assert injected is True
    assert report == {
        "files": sorted(STRICT_ARTIFACT_FILES),
        "policy": "bound-policy-source",
        "precompute": "bound-precompute-source",
        "site_marker": None,
        "sitecustomize_loaded": False,
        "isolated": 1,
        "dont_write_bytecode": True,
    }
