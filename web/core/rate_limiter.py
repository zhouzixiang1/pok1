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

# Fallback: detect 429 even without a reset timestamp
_429_INDICATOR_RE = re.compile(r'Request rejected \(429\)')

# Default wait when no reset time can be parsed (5 minutes)
_DEFAULT_WAIT_SECONDS = 300

# How often to check shutdown during wait (seconds)
_SHUTDOWN_CHECK_INTERVAL = 30


class RateLimiter:
    """Thread-safe singleton for managing 429 quota-exhaustion blocking."""

    def __init__(self, state_file=None):
        self._reset_time: float | None = None  # Unix timestamp
        self._lock = threading.Lock()
        self._state_file = state_file or RESULTS_DIR / "rate_limit_state.json"
        self._load_state()

    def parse_429(self, error_text: str) -> bool:
        """Parse reset timestamp from 429 error text.

        Returns True if a reset time was extracted (or a default was set).
        Returns False if the text doesn't look like a 429 error.
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

            # Fallback: detect 429 without reset time
            if _429_INDICATOR_RE.search(error_text):
                self._reset_time = time.time() + _DEFAULT_WAIT_SECONDS
                self._save_state()
                log.info("429 detected (no reset time). Defaulting to %ds wait.", _DEFAULT_WAIT_SECONDS)
                return True

            # Check for Chinese-only pattern without "Request rejected"
            if "已达到" in error_text and "使用上限" in error_text:
                self._reset_time = time.time() + _DEFAULT_WAIT_SECONDS
                self._save_state()
                log.info("429 detected (Chinese pattern). Defaulting to %ds wait.", _DEFAULT_WAIT_SECONDS)
                return True

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
        and orchestrator_loop().
        """
        while self.is_blocked():
            wait = self.wait_seconds()
            if wait <= 0:
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
