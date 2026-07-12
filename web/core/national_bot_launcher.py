"""Static helpers for the native national bot entry contract.

Subprocess construction deliberately does not live here.  Every executable
native bot is launched by :mod:`managed_bot_executor`, which owns namespace,
filesystem, environment, seccomp, resource-limit, endpoint, and descriptor
lifetime policy as one indivisible operation.
"""

from __future__ import annotations

from pathlib import Path


def native_entry_supports_log_arg(entry: str | Path) -> bool:
    try:
        text = Path(entry).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "--log" in text and (
        'add_argument("--log"' in text or "add_argument('--log'" in text
    )


__all__ = ["native_entry_supports_log_arg"]
