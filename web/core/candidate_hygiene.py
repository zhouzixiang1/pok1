"""Candidate directory hygiene for in-progress bot generations.

Generation tools create bot directories by copying completed parents. Parent
metadata such as ``.completed`` is authoritative only after ``commit_bot`` has
created the git commit and tag, so it must never leak into an in-progress
candidate. Native national workflows also require the formal TCP entry to be
present even if an LLM rewrites the target directory during crossover.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def sanitize_candidate_dir(
    bot_dir: str | Path,
    *,
    require_native_tcp: bool = False,
    overwrite_native_entry: bool = False,
) -> dict[str, Any]:
    """Remove parent completion metadata and restore required native entry.

    Returns a small audit payload that callers can include in logs/tests.
    """

    root = Path(bot_dir)
    result: dict[str, Any] = {
        "bot_dir": str(root),
        "completed_removed": False,
        "native_entry": None,
    }

    sentinel = root / ".completed"
    if sentinel.exists():
        sentinel.unlink()
        result["completed_removed"] = True

    if require_native_tcp:
        from national_native import ensure_native_entry

        entry = ensure_native_entry(root, overwrite=overwrite_native_entry)
        result["native_entry"] = entry.name

    return result
