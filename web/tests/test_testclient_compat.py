"""Regression coverage for the Python 3.14 Starlette TestClient workaround."""

from __future__ import annotations

import pytest

import testclient_compat as compat


def test_linux_python314_uses_uvloop_portal(monkeypatch):
    monkeypatch.setattr(compat.sys, "platform", "linux")
    monkeypatch.setattr(compat.sys, "version_info", (3, 14, 0))
    monkeypatch.setattr(compat.importlib, "import_module", lambda name: object())

    assert compat.backend_options_for_testclient() == {"use_uvloop": True}


def test_linux_python314_without_uvloop_fails_fast(monkeypatch):
    monkeypatch.setattr(compat.sys, "platform", "linux")
    monkeypatch.setattr(compat.sys, "version_info", (3, 14, 0))

    def unavailable(_name):
        raise ImportError("uvloop unavailable")

    monkeypatch.setattr(compat.importlib, "import_module", unavailable)

    with pytest.raises(RuntimeError, match="require uvloop"):
        compat.backend_options_for_testclient()


def test_other_test_hosts_keep_default_portal(monkeypatch):
    monkeypatch.setattr(compat.sys, "platform", "linux")
    monkeypatch.setattr(compat.sys, "version_info", (3, 13, 9))

    assert compat.backend_options_for_testclient() == {}
