"""
Experience Pool Management.

Provides functions to trim and maintain the experience_pool.md file,
preventing unbounded growth and stale advice.
"""

import re
import fcntl
from pathlib import Path

EXPERIENCE_FILE = Path(__file__).resolve().parent / "experience_pool.md"


def trim_experience_pool(max_entries=8):
    """Keep only the most recent N generation entries in experience_pool.md.

    Splits the file by generation headers (e.g. '#### Generation claude_v6 → claude_v7'),
    keeps the last `max_entries` blocks, and rewrites the file.
    """
    if not EXPERIENCE_FILE.exists():
        return

    with open(EXPERIENCE_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        content = f.read()

        # Split by generation headers like "- **v6 -> v7**:" or "#### Generation claude_v6 → claude_v7"
        # The experience pool uses "- **v{X} -> v{Y}**:" format for each generation's summary
        pattern = r'(- \*\*v\d+ -> v\d+\*\*:)'
        parts = re.split(pattern, content)

        if len(parts) <= 1:
            # No generation entries found, nothing to trim
            fcntl.flock(f, fcntl.LOCK_UN)
            return

        # parts[0] = header + intro text
        # parts[1], parts[2] = header, body for first entry
        # parts[3], parts[4] = header, body for second entry, etc.
        header = parts[0]
        pairs = [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)]

        if len(pairs) <= max_entries:
            fcntl.flock(f, fcntl.LOCK_UN)
            return  # Already within limit

        kept = pairs[-max_entries:]
        result = header + ''.join(h + b for h, b in kept)
        f.seek(0)
        f.truncate()
        f.write(result)
        fcntl.flock(f, fcntl.LOCK_UN)
