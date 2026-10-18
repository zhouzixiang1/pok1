"""Global 429 rate-limit handler for LLM API quota exhaustion.

Provides a singleton `rate_limiter` that:
- Parses reset timestamps from Chinese-language 429 error messages
- Blocks all LLM calls until the reset time
- Persists state to disk for crash recovery
- Supports graceful shutdown during the wait period

The GLM API returns errors like:
    "Request rejected (429) · [1308][已达到 5 小时的使用上限。
     您的限额将在 2026-06-07 16:20:12 重置。][...]"

Usage:
    from rate_limiter import rate_limiter

    if rate_limiter.is_blocked():
        await rate_limiter.wait_until_reset(shutdown_mgr)

    # Or detect from error text:
    rate_limiter.parse_429(error_text)
"""

import asyncio
import json
import logging
import os
import re
import time
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger("pok.rate_limiter")

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Regex to extract reset timestamp from Chinese 429 error messages.
# Matches: "限额将在 2026-06-07 16:20:12 重置" or "限额将在2026-06-07 16:20:12重置"
_RESET_TIME_RE = re.compile(
    r'限额将在\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*重置'
)

# Detect a 429 even without a reset timestamp. Detection alone is not resume
# authority; the durable availability store requires manual acknowledgement.
_429_INDICATOR_RE = re.compile(r'Request rejected \(429\)')

# How often to check shutdown during wait (seconds)
_SHUTDOWN_CHECK_INTERVAL = 30

# ---------------------------------------------------------------------------
# Quota-wait provider probing (2026-10-18): the provider's declared reset
# timestamp stays the DEFAULT resume authority, but the actual 5-hour window
# can re-open before it (observed 2026-10-18: the provider resumed while the
# persisted pause still had hours left, parking the pipeline for nothing).
# While blocked, send one minimal 1-token request every probe interval; a
# single acceptance means the window reset atomically provider-side.  Every
# rejection/error is "still blocked" — probing never extends or re-arms
# anything.
# ---------------------------------------------------------------------------

_QUOTA_PROBE_TIMEOUT_SEC = 15.0


def _quota_probe_enabled() -> bool:
    return os.environ.get("POK_QUOTA_PROBE_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


def _quota_probe_interval_sec() -> float:
    try:
        return max(60.0, float(os.environ.get("POK_QUOTA_PROBE_INTERVAL_SEC", "300")))
    except (TypeError, ValueError):
        return 300.0


def _probe_provider_quota_once() -> dict | None:
    """One blocking minimal provider request. True status only on HTTP 200.

    Returns {"ok": bool, "http_status": int, "elapsed_sec": float, "model":
    str} or None on transport/auth failure (never permission to resume).
    """

    import urllib.error
    import urllib.request

    base = str(os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    model = str(os.environ.get("ANTHROPIC_MODEL") or "")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not base or not model or not (auth_token or api_key):
        return None
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }
    ).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    if api_key:
        headers["x-api-key"] = api_key
    request = urllib.request.Request(
        f"{base}/v1/messages", data=body, headers=headers, method="POST"
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=_QUOTA_PROBE_TIMEOUT_SEC) as response:
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "http_status": int(exc.code),
            "elapsed_sec": time.time() - started,
            "model": model,
        }
    except Exception:
        return None
    return {
        "ok": status == 200,
        "http_status": status,
        "elapsed_sec": time.time() - started,
        "model": model,
    }


async def _probe_provider_quota_available() -> dict | None:
    """Run the blocking probe off the event loop."""

    return await asyncio.to_thread(_probe_provider_quota_once)


class RateLimiter:
    """Thread-safe singleton for managing 429 quota-exhaustion blocking."""

    def __init__(self, state_file=None):
        self._reset_time: float | None = None  # Unix timestamp
        self._lock = threading.Lock()
        self._state_file = state_file or RESULTS_DIR / "rate_limit_state.json"
        self._load_state()

    def parse_429(self, error_text: str) -> bool:
        """Parse reset timestamp from 429 error text.

        Returns True only when an explicit reset time was extracted. A bare 429
        returns False: guessing a five-minute window caused unsafe retries
        against multi-hour provider quota caps. The LLM availability pause owns
        manual recovery for that case.
        """
        if not error_text or len(error_text) > 2000:
            return False

        with self._lock:
            # Try to extract reset timestamp
            m = _RESET_TIME_RE.search(error_text)
            if m:
                try:
                    dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    reset_ts = dt.timestamp()
                    # Sanity: reset must be in the future (allow 60s clock skew)
                    if reset_ts > time.time() - 60:
                        self._reset_time = reset_ts
                        self._save_state()
                        log.info("429 parsed: quota resets at %s", dt.strftime("%Y-%m-%d %H:%M:%S"))
                        return True
                    else:
                        log.info("429 parsed but reset time %s is in the past — ignoring", m.group(1))
                        return False
                except ValueError:
                    pass

            # A recognized 429 without reset evidence deliberately remains
            # unprojected here. The durable availability store will mark it as
            # manual; this legacy helper must never invent an auto-resume time.
            if _429_INDICATOR_RE.search(error_text):
                log.warning("429 detected without provider reset evidence; no automatic retry")
                return False

            # Check for Chinese-only pattern without "Request rejected"
            if "已达到" in error_text and "使用上限" in error_text:
                log.warning("quota limit detected without provider reset evidence; no automatic retry")
                return False

            return False

    def is_blocked(self) -> bool:
        """Check if LLM calls should be blocked."""
        with self._lock:
            if self._reset_time is None:
                return False
            if time.time() >= self._reset_time:
                # Reset time passed — auto-clear
                self._reset_time = None
                self._save_state()
                return False
            return True

    def wait_seconds(self) -> float:
        """Seconds until the quota resets (0 if not blocked)."""
        with self._lock:
            if self._reset_time is None:
                return 0.0
            remaining = self._reset_time - time.time()
            if remaining <= 0:
                self._reset_time = None
                self._save_state()
                return 0.0
            return remaining

    def reset_time_str(self) -> str:
        """Human-readable reset time (empty string if not blocked)."""
        with self._lock:
            if self._reset_time is None:
                return ""
            return datetime.fromtimestamp(self._reset_time).strftime("%Y-%m-%d %H:%M:%S")

    async def wait_until_reset(self, shutdown_mgr=None):
        """Block until the quota resets. Checks shutdown_mgr every 30s.

        This is the main blocking point used by both run_claude_query()
        and orchestrator_loop().  While a trusted quota block holds, one
        minimal provider probe fires per ``POK_QUOTA_PROBE_INTERVAL_SEC``
        (default 300s); a single accepted request resumes the wait AND the
        durable availability pause early via
        ``llm_availability_store.resume_quota_pause_on_probe_success``.
        """
        last_probe = time.time()
        while self.is_blocked():
            wait = self.wait_seconds()
            if wait <= 0:
                break
            if (
                _quota_probe_enabled()
                and wait > 60.0
                and time.time() - last_probe >= _quota_probe_interval_sec()
            ):
                last_probe = time.time()
                try:
                    probe = await _probe_provider_quota_available()
                except Exception:
                    probe = None
                if probe is not None and probe.get("ok") is True:
                    log.info(
                        "Quota probe succeeded (HTTP %s, %.1fs) %.0fs ahead of the"
                        " declared reset — resuming early.",
                        probe.get("http_status"),
                        float(probe.get("elapsed_sec") or 0.0),
                        wait,
                    )
                    try:
                        from evolution_infra import log_system_event

                        log_system_event(
                            "pipeline.llm_quota_probe_resumed",
                            "success",
                            "Provider accepted a quota probe before the declared"
                            " reset; resuming early.",
                            {
                                "http_status": probe.get("http_status"),
                                "elapsed_sec": probe.get("elapsed_sec"),
                                "seconds_ahead_of_reset": round(wait),
                            },
                        )
                    except Exception:
                        pass
                    try:
                        import llm_availability_store

                        llm_availability_store.resume_quota_pause_on_probe_success(
                            probe
                        )
                    except Exception as exc:
                        log.warning(
                            "Probe-resume of the durable pause failed: %s", exc
                        )
                    break
            # Sleep in chunks so we can check shutdown
            chunk = min(wait, _SHUTDOWN_CHECK_INTERVAL)
            log.info("Rate-limited: waiting %.0fs (reset at %s)", wait, self.reset_time_str())
            if shutdown_mgr:
                try:
                    await asyncio.wait_for(
                        shutdown_mgr.wait_for_shutdown(),
                        timeout=chunk,
                    )
                    # Shutdown requested — exit wait
                    return
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(chunk)

        # Clear state after successful wait
        with self._lock:
            self._reset_time = None
            self._save_state()

    def clear(self):
        """Manually clear the rate-limit block."""
        with self._lock:
            self._reset_time = None
            self._save_state()
        log.info("Rate-limit block cleared manually.")

    def _save_state(self):
        """Persist rate-limit state to disk (atomic write)."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {"reset_time": self._reset_time}
            tmp = self._state_file.with_suffix(".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, json.dumps(data).encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(str(tmp), str(self._state_file))
        except OSError as e:
            log.warning("Failed to save rate-limit state: %s", e)

    def _load_state(self):
        """Load persisted state on startup. Clears if reset time has passed."""
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                reset_ts = data.get("reset_time")
                if reset_ts is not None:
                    if reset_ts > time.time():
                        self._reset_time = reset_ts
                        dt = datetime.fromtimestamp(reset_ts)
                        log.info(
                            "Rate-limit state recovered: blocked until %s (%.0fs remaining)",
                            dt.strftime("%Y-%m-%d %H:%M:%S"),
                            reset_ts - time.time(),
                        )
                    else:
                        log.info("Rate-limit state expired (reset was at %s) — clearing.", reset_ts)
                        self._reset_time = None
                        self._save_state()
        except (json.JSONDecodeError, OSError, TypeError) as e:
            log.warning("Failed to load rate-limit state: %s", e)
            self._reset_time = None


# Module-level singleton — all LLM call sites share this instance
rate_limiter = RateLimiter()


# Re-export for tests and callers that want to pace probes themselves.
quota_probe_enabled = _quota_probe_enabled
quota_probe_interval_sec = _quota_probe_interval_sec
