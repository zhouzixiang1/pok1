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

    def emit(self, record):
        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 1.0]
        if len(self._timestamps) >= self._max_rate:
            return
        self._timestamps.append(now)

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

    # Prevent propagation to root logger (avoids duplicate stderr output)
    root.propagate = False

    fmt = "%(asctime)s %(levelname)-8s [%(short_name)s] %(message)s"
    datefmt = "%H:%M:%S"

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

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Get a logger under the pok hierarchy. E.g. get_logger("orchestrator") -> pok.orchestrator."""
    return logging.getLogger(f"{_ROOT_LOGGER}.{name}")
