"""Archived Botzone decision-tester CLI coverage."""

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_decision_tester_help_exits_without_running_scenarios():
    proc = subprocess.run(
        [sys.executable, "web/core/decision_tester.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert proc.returncode == 0
    assert "usage:" in proc.stdout.lower()
    assert "bot_path" in proc.stdout
    assert "Critical failures" not in proc.stderr
