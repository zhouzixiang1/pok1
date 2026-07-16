"""Rotating JSONL audit log with recursive secret redaction."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from typing import Any


_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|auth[_-]?token|access[_-]?token|secret|password|private[_-]?key)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_TOKENISH = re.compile(r"(?i)\b(?:sk|api)[-_][A-Za-z0-9_-]{12,}\b")


def redact(value: Any, *, key: str = "") -> Any:
    if _SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _TOKENISH.sub("<redacted>", _BEARER.sub("Bearer <redacted>", value))
    return value


class AuditLogger:
    def __init__(self, path: Path, *, max_bytes: int, backup_count: int):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._logger = logging.getLogger(f"worker_mcp.audit.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)

    def log(self, event: str, **fields: Any) -> None:
        payload = redact({"event": event, **fields})
        self._logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def close(self) -> None:
        for handler in list(self._logger.handlers):
            handler.flush()
            handler.close()
            self._logger.removeHandler(handler)
