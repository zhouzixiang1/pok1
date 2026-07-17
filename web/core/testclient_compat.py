"""Test-only backend selection for Starlette's in-memory client.

Production routes never import this module.  The Web test runner adds
``web/core`` to its import path before loading its fixtures.
"""

from __future__ import annotations

import importlib
import sys


def backend_options_for_testclient() -> dict[str, bool]:
    """Select the safe Starlette test portal backend for this host.

    Python 3.14's default AnyIO/asyncio portal stalls in this Linux test
    environment.  Do not silently fall back to that known-bad path: the Web
    requirements already request ``uvicorn[standard]``, which supplies uvloop
    here.  Older interpreters retain the framework default so this helper does
    not change their test semantics.
    """

    if sys.platform != "linux" or sys.version_info < (3, 14):
        return {}

    try:
        importlib.import_module("uvloop")
    except ImportError as exc:
        raise RuntimeError(
            "Python 3.14 Linux Web tests require uvloop via uvicorn[standard]; "
            "refusing the known-stalling default TestClient backend"
        ) from exc
    return {"use_uvloop": True}
