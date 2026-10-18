"""Quota-wait provider probing (2026-10-18): a single accepted 1-token probe
resumes the trusted quota block AND the durable availability pause ahead of
the declared provider reset; every rejection/error leaves both untouched."""

import asyncio
import time
from types import SimpleNamespace

import pytest

import rate_limiter as rl


class _Shutdown:
    async def wait_for_shutdown(self):
        await asyncio.Event().wait()  # never


def _drive(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


def test_probe_success_resumes_wait_and_store_pause(monkeypatch, tmp_path):
    import llm_availability_store as store

    store.pause_path()  # import side effects only
    monkeypatch.setattr(
        store, "pause_path", lambda: tmp_path / "pause.json"
    )
    monkeypatch.setattr(
        store, "_results_dir", lambda: tmp_path
    )
    # A durable trusted quota pause with a still-future reset.
    future = time.time() + 7200.0
    from datetime import datetime, timezone

    def iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    (tmp_path / "pause.json").write_text(
        __import__("json").dumps(
            {
                "schema_version": 2,
                "active": True,
                "category": "quota_429",
                "summary": "已达到 5 小时的使用上限",
                "requires_manual_resume": False,
                "provider_reset_at": iso(future),
                "quota_reset_authority": "provider_timestamp",
                "evidence_digest": "a" * 64,
            }
        )
    )

    limiter = rl.RateLimiter(state_file=tmp_path / "rate_limit_state.json")
    limiter._reset_time = future
    monkeypatch.setattr(rl, "_quota_probe_enabled", lambda: True)
    monkeypatch.setattr(rl, "_quota_probe_interval_sec", lambda: 0.0)

    probes = []

    async def fake_probe():
        probes.append(1)
        return {"ok": True, "http_status": 200, "elapsed_sec": 0.3, "model": "m"}

    monkeypatch.setattr(rl, "_probe_provider_quota_available", fake_probe)

    started = time.time()
    _drive(limiter.wait_until_reset(None))
    assert time.time() - started < 5
    assert probes == [1]
    assert limiter.is_blocked() is False
    state = __import__("json").loads((tmp_path / "pause.json").read_text())
    assert state["active"] is False
    assert state["resume_source"] == "provider_probe_success"
    assert state["probe_evidence"]["http_status"] == 200


def test_probe_rejection_keeps_both_paused(monkeypatch, tmp_path):
    limiter = rl.RateLimiter(state_file=tmp_path / "rl.json")
    limiter._reset_time = time.time() + 120.0
    monkeypatch.setattr(rl, "_quota_probe_enabled", lambda: True)
    monkeypatch.setattr(rl, "_quota_probe_interval_sec", lambda: 0.0)

    async def fake_probe():
        return {"ok": False, "http_status": 429, "elapsed_sec": 0.2, "model": "m"}

    monkeypatch.setattr(rl, "_probe_provider_quota_available", fake_probe)

    # Expire the block from inside the wait so it terminates without the probe.
    original = limiter.wait_seconds

    def fast_forward():
        limiter._reset_time = time.time() - 1
        return original()

    monkeypatch.setattr(limiter, "wait_seconds", fast_forward)
    _drive(limiter.wait_until_reset(None))
    assert limiter.is_blocked() is False  # expired, not probe-resumed


def test_store_resume_requires_trusted_future_quota(monkeypatch, tmp_path):
    import json as _json

    import llm_availability_store as store

    monkeypatch.setattr(store, "pause_path", lambda: tmp_path / "p.json")
    monkeypatch.setattr(store, "_results_dir", lambda: tmp_path)
    from datetime import datetime, timezone

    def iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    base = {
        "schema_version": 2,
        "active": True,
        "category": "quota_429",
        "summary": "x",
        "requires_manual_resume": False,
        "evidence_digest": "a" * 64,
    }
    # Untrusted authority: untouched.
    row = dict(base, provider_reset_at=iso(time.time() + 3600),
               quota_reset_authority="")
    (tmp_path / "p.json").write_text(_json.dumps(row))
    out = store.resume_quota_pause_on_probe_success({"http_status": 200})
    assert out["active"] is True
    # Elapsed reset: untouched (normal reconcile owns it).
    row = dict(base, provider_reset_at=iso(time.time() - 60),
               quota_reset_authority="provider_timestamp")
    (tmp_path / "p.json").write_text(_json.dumps(row))
    out = store.resume_quota_pause_on_probe_success({"http_status": 200})
    assert out["active"] is True
    # Non-quota category: untouched.
    row = dict(base, category="service_unavailable",
               quota_reset_authority="provider_timestamp")
    (tmp_path / "p.json").write_text(_json.dumps(row))
    out = store.resume_quota_pause_on_probe_success({"http_status": 200})
    assert out["active"] is True


def test_probe_request_shape(monkeypatch):
    import urllib.request

    seen = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = request.data.decode("utf-8")
        seen["headers"] = dict(request.header_items())
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://provider.example")
    monkeypatch.setenv("ANTHROPIC_MODEL", "glm-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-1")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = rl._probe_provider_quota_once()
    assert result == {
        "ok": True, "http_status": 200, "elapsed_sec": result["elapsed_sec"],
        "model": "glm-test",
    }
    assert seen["url"] == "https://provider.example/v1/messages"
    assert '"max_tokens": 1' in seen["body"]
    assert seen["timeout"] == rl._QUOTA_PROBE_TIMEOUT_SEC
    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers.get("authorization") == "Bearer tok-1"
    assert headers.get("x-api-key") == "key-1"


def test_probe_missing_config_returns_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert rl._probe_provider_quota_once() is None
