"""Cross-file orchestrator module-identity isolation (2026-10-11 root cause).

Root cause closed here (observed as 5 failures when
``test_epoch_launch_guard.py`` ran before ``test_orchestrator_cost_policy.py``):

1. ``_last_evolution_stop_reason`` (``server/routes/control.py``) lazily
   imported ``orchestrator_stage_routing`` — a COMPANION-first import order.
   That starts the orchestrator<->companion import cycle backwards:
   ``orchestrator_stage_routing``'s module-level ``import orchestrator as _o``
   begins executing ``orchestrator.py``, whose bottom re-export block
   (``from orchestrator_stage_routing import ...``) then hits the
   partially-initialized companion and raises ``ImportError``.
2. Python removes ``orchestrator`` (and the starter companion) from
   ``sys.modules``, but companion modules that COMPLETED during the partial
   run — notably ``orchestrator_abandon_and_cost`` — survive forever holding
   ``_o`` bound to the dead partial orchestrator object.
3. When the first orchestrator import happens inside a test that monkeypatches
   ``system_log.log_system_event`` (the pre-reset status guard test), the dead
   partial object also snapshot-binds that test lambda at
   ``orchestrator.py``'s ``from system_log import log_system_event``.
4. Every later ``import orchestrator`` binds a DIFFERENT (healthy) module
   object, so ``monkeypatch.setattr(orchestrator, "log_system_event", ...)``
   in the cost-policy tests never reached the moved bodies' ``_o.<name>``
   reads — they executed the poisoned epoch-guard lambda and failed with
   ``pre-reset status persisted an event``.

Fixes under test (all 2026-10-11):
- ``server/routes/control.py::_last_evolution_stop_reason`` imports
  ``orchestrator`` BEFORE ``orchestrator_stage_routing``.
- ``web/core/disk_hygiene.py::_rotate_orchestrator_logs`` imports
  ``orchestrator`` BEFORE ``orchestrator_abandon_and_cost``.
- ``web/tests/conftest.py`` eagerly imports ``orchestrator`` before any test,
  so no monkeypatch window can ever contain the cluster's first import.

The production-order tests run in FRESH SUBPROCESSES (no conftest eager
import), reproducing a view-only / reset-required server where the /health
observer build or the disk-hygiene janitor runs before ``orchestrator`` has
ever been imported. Before the fix, that order raised the circular
ImportError (swallowed at the seam) and left the poisoned companion cluster.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_SUBPROCESS_PRELUDE = textwrap.dedent(
    """
    import os, sys
    os.environ.setdefault("POK_CLOUD_RUNTIME", "1")
    for _p in ({roots!r}):
        sys.path.insert(0, _p)
    """
)


def _run_isolated_python(body: str) -> subprocess.CompletedProcess:
    roots = [
        str(PROJECT_ROOT),
        str(PROJECT_ROOT / "web"),
        str(PROJECT_ROOT / "web" / "core"),
        str(PROJECT_ROOT / "web" / "server"),
    ]
    script = _SUBPROCESS_PRELUDE.format(roots=roots) + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(PROJECT_ROOT),
    )


def test_health_seam_does_not_poison_companion_bindings_20261011():
    """Production order: /health before orchestrator import must stay clean."""

    result = _run_isolated_python(
        """
        import server.routes.control as control

        # Precondition: this seam is reachable before orchestrator ever loads
        # (view-only / reset-required server). If control.py ever starts
        # importing orchestrator at module level this assertion documents the
        # changed precondition rather than silently weakening the test.
        assert "orchestrator" not in sys.modules, (
            "precondition drifted: orchestrator already imported"
        )

        control._last_evolution_stop_reason()

        import orchestrator
        import orchestrator_abandon_and_cost
        import system_log

        assert orchestrator_abandon_and_cost._o is orchestrator, (
            "companion _o is a stale partial orchestrator module object"
        )
        assert (
            orchestrator_abandon_and_cost._o.log_system_event
            is orchestrator.log_system_event
            is system_log.log_system_event
        ), "companion _o.log_system_event diverged from the live module"
        print("OK")
        """
    )
    assert result.returncode == 0, (
        f"health-seam subprocess failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_disk_hygiene_rotate_seam_does_not_poison_companion_bindings_20261011(tmp_path):
    """Production order: lifespan janitor rotation before orchestrator import."""

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    result = _run_isolated_python(
        """
        from pathlib import Path

        import disk_hygiene

        assert "orchestrator" not in sys.modules, (
            "precondition drifted: orchestrator already imported"
        )

        freed, removed = disk_hygiene._rotate_orchestrator_logs(
            Path(__LOGS_DIR__), keep=1
        )
        assert (freed, removed) == (0, 0)

        import orchestrator
        import orchestrator_abandon_and_cost
        import system_log

        assert orchestrator_abandon_and_cost._o is orchestrator, (
            "companion _o is a stale partial orchestrator module object"
        )
        assert (
            orchestrator_abandon_and_cost._o.log_system_event
            is orchestrator.log_system_event
            is system_log.log_system_event
        ), "companion _o.log_system_event diverged from the live module"
        print("OK")
        """.replace("__LOGS_DIR__", repr(str(logs_dir)))
    )
    assert result.returncode == 0, (
        f"disk-hygiene-seam subprocess failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_in_process_cluster_identity_holds_20261011():
    """The conftest eager import keeps one live orchestrator for everything."""

    import orchestrator
    import orchestrator_abandon_and_cost
    import orchestrator_branch_guard
    import orchestrator_post_generation
    import orchestrator_stage_routing
    import system_log

    live = sys.modules["orchestrator"]
    assert live is orchestrator
    for companion in (
        orchestrator_abandon_and_cost,
        orchestrator_branch_guard,
        orchestrator_post_generation,
        orchestrator_stage_routing,
    ):
        assert companion._o is live, (
            f"{companion.__name__}._o diverged from the live orchestrator"
        )
    assert orchestrator.log_system_event is system_log.log_system_event, (
        "orchestrator.log_system_event is an import-time snapshot of a "
        "patched function (monkeypatch leaked across test files)"
    )
