"""Fail-closed sandbox tests for the strict five-file policy artifact."""

import socket
from pathlib import Path

import pytest

from bot_namespace import STRICT_ARTIFACT_FILES, strict_artifact_layout_errors


def _write_strict_bot(root: Path, policy_source: str) -> Path:
    root.mkdir()
    payloads = {
        "national_bot.py": "# system runtime is not imported by this probe\n",
        "precompute.py": "FACT = 1\n",
        "policy.py": policy_source,
        "national_runtime_manifest.json": "{}\n",
        "policy_epoch_receipt.json": "{}\n",
    }
    assert frozenset(payloads) == STRICT_ARTIFACT_FILES
    for relative, payload in payloads.items():
        (root / relative).write_text(payload, encoding="utf-8")
    assert strict_artifact_layout_errors(root) == []
    return root


def test_policy_import_cannot_write_candidate_or_host_files(tmp_path):
    import code_verification

    host_marker = tmp_path / "host-side-effect.txt"
    bot = _write_strict_bot(
        tmp_path / "national_v143",
        "from pathlib import Path\n"
        "for target in (Path('/work/policy-side-effect.txt'), "
        f"Path({str(host_marker)!r})):\n"
        "    try:\n"
        "        target.write_text('escaped', encoding='utf-8')\n"
        "    except OSError:\n"
        "        pass\n"
        "def get_baseline_decision(context):\n"
        "    return {'kind': 'pass'}\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    return iter(())\n",
    )

    assert code_verification.run_import_contract_test(bot, timeout=5) == []
    assert not (bot / "policy-side-effect.txt").exists()
    assert not host_marker.exists()


def test_policy_import_cannot_reach_host_loopback(tmp_path):
    import code_verification

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.25)
    port = listener.getsockname()[1]
    bot = _write_strict_bot(
        tmp_path / "national_v143",
        "import socket\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=1)\n"
        "except OSError:\n"
        "    NETWORK_BLOCKED = True\n"
        "else:\n"
        "    NETWORK_BLOCKED = False\n"
        "def get_baseline_decision(context):\n"
        "    return {'kind': 'pass'}\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    return iter(())\n",
    )

    try:
        assert code_verification.run_import_contract_test(bot, timeout=5) == []
        with pytest.raises((TimeoutError, socket.timeout)):
            listener.accept()
    finally:
        listener.close()


def test_policy_import_isolation_failure_has_no_host_fallback(
    monkeypatch, tmp_path
):
    import candidate_sandbox
    import code_verification
    from managed_bot_executor import IsolationUnavailable

    bot = _write_strict_bot(
        tmp_path / "national_v143",
        "def get_baseline_decision(context):\n"
        "    return {'kind': 'pass'}\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    return iter(())\n",
    )

    def unavailable(*_args, **_kwargs):
        raise IsolationUnavailable("test_bwrap_missing")

    monkeypatch.setattr(candidate_sandbox, "launch_isolated_worker", unavailable)
    with pytest.raises(candidate_sandbox.CandidateSandboxError) as captured:
        code_verification.run_import_contract_test(bot, timeout=5)
    assert "isolation_unavailable" in str(captured.value)


def test_unhandled_policy_import_write_is_candidate_failure_without_side_effect(
    tmp_path,
):
    import code_verification

    bot = _write_strict_bot(
        tmp_path / "national_v143",
        "from pathlib import Path\n"
        "Path('/work/forbidden.txt').write_text('bad', encoding='utf-8')\n",
    )

    errors = code_verification.run_import_contract_test(bot, timeout=5)

    assert errors
    assert errors[0]["module"] == "policy"
    assert errors[0]["exception"] in {"OSError", "PermissionError"}
    assert not (bot / "forbidden.txt").exists()


def test_policy_cannot_forge_import_receipt_then_exit_before_trusted_completion(
    tmp_path,
):
    import code_verification

    bot = _write_strict_bot(
        tmp_path / "national_v143",
        "import json, os\n"
        "print(json.dumps({\n"
        "    'schema': 'strict-policy-import-v1',\n"
        "    'ok': True,\n"
        "    'modules': ['precompute', 'policy'],\n"
        "}), flush=True)\n"
        "os._exit(0)\n",
    )

    errors = code_verification.run_import_contract_test(bot, timeout=5)

    assert errors
    assert errors[0]["exception"] == "ProcessExit0"
