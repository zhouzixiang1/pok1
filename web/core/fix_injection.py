"""Centralized fix registry and application engine for bot code fixes.

Known critical fixes that must be present in every bot generation.
Applied automatically after prepare_next_gen, run_crossover, and worker retry.

Each fix uses idempotent search-and-replace with guard checks.
If a fix's search string is not found, it is logged as skipped for visibility.
"""

import fcntl
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("pok.fixes")


@dataclass
class Patch:
    """A single file patch within a fix."""
    file_rel: str          # relative path inside bot dir, e.g. "card_utils.py"
    search: str            # exact text to search for (must match verbatim)
    replace: str           # replacement text
    guard: str | None = None  # if present in file, skip this patch (idempotency)


@dataclass
class Fix:
    """A named fix composed of one or more patches."""
    fix_id: str
    description: str
    patches: list[Patch]
    active: bool = True


# ──────────────────────────────────────────────
# Fix registry
# ──────────────────────────────────────────────

MANDATORY_FIXES: list[Fix] = [
    Fix(
        fix_id="BOT-001a",
        description="Wheel straight (A-2-3-4-5) in card_utils.py evaluate_5()",
        patches=[
            Patch(
                file_rel="card_utils.py",
                search=(
                    "        if unique_ranks[0] - unique_ranks[4] == 4:\n"
                    "            is_straight = True\n"
                    "            straight_high = unique_ranks[0]\n\n"
                    "    if is_flush and is_straight:"
                ),
                replace=(
                    "        if unique_ranks[0] - unique_ranks[4] == 4:\n"
                    "            is_straight = True\n"
                    "            straight_high = unique_ranks[0]\n"
                    "        # Wheel straight: A-2-3-4-5\n"
                    "        elif set(unique_ranks) == {14, 2, 3, 4, 5}:\n"
                    "            is_straight = True\n"
                    "            straight_high = 5\n\n"
                    "    if is_flush and is_straight:"
                ),
                guard="{14, 2, 3, 4, 5}",
            ),
        ],
    ),
    Fix(
        fix_id="BOT-002a",
        description="Re-raise minimum: strictly > 2x (last_raise_to variant)",
        patches=[
            Patch(
                file_rel="state.py",
                search="    min_raise_action = max(0, 2 * last_raise_to - my_round_bet)",
                replace="    min_raise_action = max(0, 2 * last_raise_to + 1 - my_round_bet)",
                guard="2 * last_raise_to + 1 - my_round_bet",
            ),
        ],
    ),
    Fix(
        fix_id="BOT-002b",
        description="Re-raise minimum: strictly > 2x (judge_round_raise variant, older bots)",
        patches=[
            Patch(
                file_rel="state.py",
                search="    min_raise_action = max(0, 2 * judge_round_raise - my_round_bet)",
                replace="    min_raise_action = max(0, 2 * judge_round_raise + 1 - my_round_bet)",
                guard="2 * judge_round_raise + 1 - my_round_bet",
            ),
        ],
        active=False,  # Dead template: no evolved bot uses judge_round_raise
    ),
    Fix(
        fix_id="BOT-004",
        description="TOTAL_HANDS must be 70 (not 50)",
        patches=[
            Patch(
                file_rel="constants.py",
                search="TOTAL_HANDS = 50",
                replace="TOTAL_HANDS = 70",
                guard="TOTAL_HANDS = 70",
            ),
        ],
    ),
]


def _locked_read_write(path: Path, new_content: str) -> None:
    """Atomically write *new_content* to *path* under LOCK_EX."""
    with open(path, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        f.write(new_content)
        f.truncate()
        f.flush()
        import os
        os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def apply_known_fixes(bot_dir: Path) -> tuple[list[str], list[str]]:
    """Apply all active fixes to *bot_dir*.

    Returns (applied_fix_ids, skipped_fix_ids).
    A fix is "applied" if at least one of its patches was applied.
    A fix is "skipped" if ALL of its patches were skipped (guard present or search not found).
    """
    applied: list[str] = []
    skipped: list[str] = []

    for fix in MANDATORY_FIXES:
        if not fix.active:
            continue

        fix_applied = False
        fix_skipped = True

        for patch in fix.patches:
            target = bot_dir / patch.file_rel
            if not target.exists():
                log.warning("Fix %s patch target missing: %s", fix.fix_id, target)
                continue

            content = target.read_text()

            # Guard check: if fixed code already present, skip
            if patch.guard and patch.guard in content:
                continue

            # Search check: if search string not found, skip
            if patch.search not in content:
                log.warning(
                    "Fix %s search not found in %s",
                    fix.fix_id, patch.file_rel,
                )
                continue

            # Apply patch
            new_content = content.replace(patch.search, patch.replace, 1)
            if new_content == content:
                log.warning("Fix %s replacement had no effect in %s", fix.fix_id, patch.file_rel)
                continue

            _locked_read_write(target, new_content)
            fix_applied = True
            fix_skipped = False
            log.info("Applied fix %s to %s", fix.fix_id, patch.file_rel)

        if fix_applied:
            applied.append(fix.fix_id)
        elif fix_skipped:
            skipped.append(fix.fix_id)

    return applied, skipped


def log_fix_application(
    applied: list[str],
    skipped: list[str],
    bot_dir: Path,
    source_v: int,
) -> None:
    """Log fix application results to system events."""
    from system_log import log_system_event

    severity = "warn" if skipped and applied else "info"
    msg_parts = []
    if applied:
        msg_parts.append(f"Applied fixes: {', '.join(applied)}")
    if skipped:
        msg_parts.append(f"Skipped fixes: {', '.join(skipped)}")

    log_system_event(
        "pipeline.fixes_applied",
        severity,
        f"Fix injection for {bot_dir.name} from v{source_v}: " + "; ".join(msg_parts),
        {
            "bot_dir": str(bot_dir.name),
            "source_v": source_v,
            "applied": applied,
            "skipped": skipped,
        },
    )
