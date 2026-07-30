"""Regression: script-launched elo_daemon must share one module with companions.

``daemon_management.start_daemon`` launches ``python …/elo_daemon.py``, so the
parent file is bound as ``__main__``. Companions do ``import elo_daemon as _ed``.
Without an early ``sys.modules`` alias those imports dual-load a twin whose
``daemon_evaluation_identity_digest`` stays ``None``, and every completed
70-hand match fails admission with
``staged match identity no longer matches the daemon evaluation epoch``.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "core"
DAEMON_SCRIPT = CORE_DIR / "elo_daemon.py"
PROJECT_PYTHON = Path(os.environ.get("PYTHON") or sys.executable)


def test_source_registers_elo_daemon_alias_before_companion_imports():
    """Guard the load-bearing alias placement in source order."""
    source = DAEMON_SCRIPT.read_text(encoding="utf-8")
    alias_idx = source.index('sys.modules.setdefault("elo_daemon"')
    admission_import_idx = source.index("import elo_daemon_admission")
    assert alias_idx < admission_import_idx, (
        "script-launch elo_daemon alias must precede companion imports"
    )
    assert 'if __name__ == "__main__":' in source[:alias_idx]


def test_script_style_load_shares_module_with_admission_companion():
    """``__main__`` script load and ``elo_daemon_admission._ed`` are one object."""
    env = {
        **os.environ,
        "POK_CLOUD_RUNTIME": "1",
        "POK_BOT_PREFIX": "national_cloud_v",
    }
    probe = r"""
import importlib.util
import sys
from pathlib import Path

core = Path(%r)
sys.path.insert(0, str(core))
sys.argv = ["elo_daemon.py", "--help"]
for key in list(sys.modules):
    if key == "elo_daemon" or key.startswith("elo_daemon"):
        del sys.modules[key]

spec = importlib.util.spec_from_file_location("__main__", core / "elo_daemon.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["__main__"] = mod
try:
    spec.loader.exec_module(mod)
except SystemExit:
    pass

import elo_daemon as ed
import elo_daemon_admission as eda

assert mod is ed, (id(mod), id(ed))
assert eda._ed is mod, (id(eda._ed), id(mod))
mod.daemon_evaluation_identity_digest = "script-alias-probe"
assert eda._ed.daemon_evaluation_identity_digest == "script-alias-probe"
print("script_module_alias_ok")
""" % (str(CORE_DIR),)
    completed = subprocess.run(
        [str(PROJECT_PYTHON), "-c", probe],
        cwd=str(CORE_DIR.parent.parent),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
    )
    assert "script_module_alias_ok" in completed.stdout
