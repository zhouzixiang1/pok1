"""
Experience Pool Management.

Provides functions to trim and maintain the experience_pool.md file,
preventing unbounded growth and stale advice.
"""

import fcntl
from pathlib import Path

EXPERIENCE_FILE = Path(__file__).resolve().parent / "experience_pool.md"


MAX_EXPERIENCE_LINES = 120
KEEP_EXPERIENCE_LINES = 100


def trim_experience_pool(max_entries=8):  # max_entries kept for call-site compatibility
    """Keep experience_pool.md under MAX_EXPERIENCE_LINES by dropping oldest lines.

    The old regex-based approach only matched '- **vX -> vY**:' entry headers,
    which didn't match LLM-consolidated content using '###' section headers.
    This line-count approach works regardless of format.
    """
    if not EXPERIENCE_FILE.exists():
        return

    with open(EXPERIENCE_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            content = f.read()
            lines = content.split("\n")
            if len(lines) <= MAX_EXPERIENCE_LINES:
                return
            kept = "\n".join(lines[-KEEP_EXPERIENCE_LINES:])
            f.seek(0)
            f.truncate()
            f.write(kept)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
