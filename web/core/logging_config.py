"""Centralized logging configuration for the Poker Evolution framework.

Provides structured logging with colored console output, rotating file logs,
and optional SSE broadcasting to the dashboard.

Usage:
    from logging_config import configure_logging
    configure_logging()                    # defaults: INFO level, logs/app.log
    configure_logging(dev_mode=True)       # DEBUG level
    configure_logging(broadcaster=bcast)   # also sends log events via SSE
"""

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_ROOT_LOGGER = "pok"
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_APP_LOG = _LOG_DIR / "app.log"
_configured = False


class ColoredConsoleFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[0m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        short = record.name.split(".")[-1] if record.name.startswith(_ROOT_LOGGER + ".") else record.name
        record.short_name = short
        msg = super().format(record)
        return f"{color}{msg}{self.RESET}"


class SSEHandler(logging.Handler):
    """Bridge Python logging to EventBroadcaster for SSE streaming to dashboard."""

    def __init__(self, broadcaster, max_rate=10):
        super().__init__(level=logging.INFO)
        self._broadcaster = broadcaster
        self._max_rate = max_rate
        self._timestamps = []
        self._drop_count = 0
        # Throttle summary cadence: every N dropped INFO/DEBUG events we emit
        # one aggregated "log_event_dropped" notice so the dashboard knows the
        # stream is shedding noise without flooding it.
        self._drop_summary_every = 20

    def _broadcast_record(self, record):
        level_map = {
            logging.DEBUG: "debug",
            logging.INFO: "info",
            logging.WARNING: "warn",
            logging.ERROR: "error",
            logging.CRITICAL: "error",
        }
        self._broadcaster.broadcast("log_event", {
            "level": level_map.get(record.levelno, "info"),
            "logger": record.name,
            "msg": self.format(record),
        })

    def emit(self, record):
        # Level-priority throttling: ERROR/CRITICAL always pass through.
        # Critic failures, cycle timeouts, and other genuine errors must never
        # be silently dropped when INFO/DEBUG noise saturates the rate budget.
        if record.levelno >= logging.ERROR:
            try:
                self._broadcast_record(record)
            except Exception:
                pass
            return

        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 1.0]
        if len(self._timestamps) >= self._max_rate:
            # Dropped a low-severity event; periodically emit an aggregate
            # notice so the dashboard surfaces the throttling.
            self._drop_count += 1
            if self._drop_count % self._drop_summary_every == 0:
                try:
                    self._broadcaster.broadcast("log_event_dropped", {
                        "level": "warn",
                        "logger": record.name,
                        "msg": "SSE handler throttled %d INFO/DEBUG events (max_rate=%d/s)"
                               % (self._drop_count, self._max_rate),
                    })
                except Exception:
                    pass
            return
        self._timestamps.append(now)

        try:
            self._broadcast_record(record)
        except Exception:
            pass


class CorrelationFilter(logging.Filter):
    """Inject run_id + pid into every LogRecord so app.log lines carry the
    generation correlation key and the originating process (RC6).

    Attached to the root ``pok`` logger, so all 25+ modules that call
    ``logging.getLogger('pok.*')`` directly (bypassing event_bus) still get
    pid + run_id in their log lines for free — no per-call-site change needed.
    """

    def filter(self, record):
        try:
            from event_bus import _run_id_cv, _last_known
            rid = _run_id_cv.get()
            if not rid:
                rid = _last_known.get("run_id")
            record.run_id = rid or "-"
        except Exception:
            record.run_id = getattr(record, "run_id", "-")
        record.pid = os.getpid()
        return True


def configure_logging(
    level="INFO",
    log_dir=None,
    broadcaster=None,
    dev_mode=False,
    quiet=False,
):
    """Configure the pok logging hierarchy. Call once at startup."""
    global _configured
    if _configured:
        return

    root = logging.getLogger(_ROOT_LOGGER)
    effective_level = logging.DEBUG if dev_mode else getattr(logging, level.upper(), logging.INFO)
    root.setLevel(effective_level)

    # Inject pid + run_id into every record so app.log lines are attributable to
    # a process + generation (RC6). Attached at the root so every handler
    # (console/file/SSE) and all 25+ bare getLogger('pok.*') users inherit it.
    correl_added = any(isinstance(f, CorrelationFilter) for f in root.filters)
    if not correl_added:
        root.addFilter(CorrelationFilter())

    # Prevent propagation to root logger (avoids duplicate stderr output)
    root.propagate = False

    # pid + run_id are injected by CorrelationFilter (attached to the root
    # logger below). RC6: previously app.log had no PID and no date, so
    # daemon/orchestrator/web lines were indistinguishable and unjoinable with
    # system_events. Date added to disambiguate across days.
    fmt = "%(asctime)s %(levelname)-8s [pid=%(pid)s v%(run_id)s] [%(short_name)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # Console handler
    if not quiet:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(ColoredConsoleFormatter(fmt, datefmt=datefmt))
        console.setLevel(effective_level)
        root.addHandler(console)

    # Rotating file handler
    ldir = Path(log_dir) if log_dir else _LOG_DIR
    ldir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(ldir / "app.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    fh.setLevel(effective_level)
    root.addHandler(fh)

    # SSE handler (web mode only)
    if broadcaster is not None:
        sse = SSEHandler(broadcaster)
        sse.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(sse)

    # Quieten high-churn subsystem loggers. These inherit the pok root's INFO
    # level by default, which floods app.log (root cause 3: ~57% of app.log was
    # scheduler/webui/workers INFO noise). Only WARNING+ reach the shared
    # handlers from these loggers now; everything else still works normally in
    # dev_mode (root DEBUG) via their own effective level.
    for noisy in ("pok.scheduler", "pok.webui", "pok.workers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Get a logger under the pok hierarchy. E.g. get_logger("orchestrator") -> pok.orchestrator."""
    return logging.getLogger(f"{_ROOT_LOGGER}.{name}")
