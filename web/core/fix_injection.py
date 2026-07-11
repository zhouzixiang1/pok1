"""Centralized fix registry and application engine for bot code fixes.

Known critical fixes that must be present in every bot generation.
Applied automatically after prepare_next_gen, run_crossover, and worker retry.

Each fix uses idempotent search-and-replace with guard checks.
If a fix's search string is not found in a relevant legacy template, it is
logged as skipped for visibility. Native national bots may not carry legacy
helper files such as card_utils.py/constants.py/postflop.py; those patches are
classified as not applicable instead of noisy skipped fixes.
"""

import fcntl
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from bot_namespace import parse_bot_version

log = logging.getLogger("pok.fixes")


@dataclass
class Patch:
    """A single file patch within a fix."""
    file_rel: str          # relative path inside bot dir, e.g. "card_utils.py"
    search: str            # exact text to search for (must match verbatim)
    replace: str           # replacement text
    guard: str | None = None  # if present in file, skip this patch (idempotency)
    relevance_markers: tuple[str, ...] = ()
    regex: bool = False
    replace_all: bool = False


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
                relevance_markers=("evaluate_5", "is_straight", "straight_high"),
            ),
        ],
    ),
    Fix(
        fix_id="BOT-002a",
        description="Conservative re-raise headroom: official 2x floor plus one chip (last_raise_to variant)",
        patches=[
            Patch(
                file_rel="state.py",
                search="    min_raise_action = max(0, 2 * last_raise_to - my_round_bet)",
                replace="    min_raise_action = max(0, 2 * last_raise_to + 1 - my_round_bet)",
                guard="2 * last_raise_to + 1 - my_round_bet",
                relevance_markers=("min_raise_action", "last_raise_to"),
            ),
        ],
    ),
    Fix(
        fix_id="BOT-002b",
        description="Conservative re-raise headroom: official 2x floor plus one chip (judge_round_raise variant, older bots)",
        patches=[
            Patch(
                file_rel="state.py",
                search="    min_raise_action = max(0, 2 * judge_round_raise - my_round_bet)",
                replace="    min_raise_action = max(0, 2 * judge_round_raise + 1 - my_round_bet)",
                guard="2 * judge_round_raise + 1 - my_round_bet",
                relevance_markers=("min_raise_action", "judge_round_raise"),
            ),
        ],
        active=False,  # DEPRECATED dead template: no evolved bot uses judge_round_raise (all claude_v* use last_raise_to variant). Kept inactive in registry for historical reference; do not re-enable. Tests assert its presence+inactive status, so do NOT delete without updating tests.
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
                relevance_markers=("TOTAL_HANDS",),
            ),
        ],
    ),
    Fix(
        fix_id="BOT-005",
        description="disciplined_opp_river_margin self-test matches widened v296 standard bucket",
        patches=[
            Patch(
                file_rel="postflop.py",
                search=(
                    "    Standard-bucket (vpip>=0.58, pfr>=0.28) returns exactly 0.0 by construction —\n"
                    "    long-tail H2H is unaffected.\n"
                ),
                replace=(
                    "    Standard-bucket (vpip>=0.62, pfr>=0.32) returns exactly 0.0 by construction —\n"
                    "    long-tail H2H is unaffected.\n"
                ),
                guard="Standard-bucket (vpip>=0.62, pfr>=0.32)",
                relevance_markers=("disciplined_opp_river_margin", "Standard-bucket", "std_om"),
            ),
            Patch(
                file_rel="postflop.py",
                search=(
                    "    # Fixture A — standard-bucket defaults (vpip/pfr at priors): delta MUST be 0\n"
                    "    std_om = {\"vpip\": 0.58, \"pfr\": 0.28, \"confidence\": 0.5}\n"
                ),
                replace=(
                    "    # Fixture A — widened standard-bucket boundary: delta MUST be 0\n"
                    "    std_om = {\"vpip\": 0.62, \"pfr\": 0.32, \"confidence\": 0.5}\n"
                ),
                guard='std_om = {"vpip": 0.62, "pfr": 0.32',
                relevance_markers=("disciplined_opp_river_margin", "Standard-bucket", "std_om"),
            ),
        ],
    ),
    Fix(
        fix_id="BOT-006",
        description="National heads-up position identity: dealer is SB and non-dealer is BB",
        patches=[
            Patch(
                file_rel="opponent.py",
                search=(
                    r"(?m)^(?P<indent>\s*)(?P<var>sb|[a-z_][a-z0-9_]*_sb)\s*=\s*"
                    r"next_player\(\s*(?P<dealer>(?=[a-z_][a-z0-9_]*dealer|dealer)[a-z_][a-z0-9_]*)\s*,\s*1\s*\)"
                ),
                replace=r"\g<indent>\g<var> = \g<dealer>",
                guard="position_semantics_regex_sb",
                relevance_markers=("next_player(dealer_id, 1)", "next_player(future_dealer, 1)"),
                regex=True,
                replace_all=True,
            ),
            Patch(
                file_rel="opponent.py",
                search=(
                    r"(?m)^(?P<indent>\s*)(?P<var>bb|[a-z_][a-z0-9_]*_bb)\s*=\s*"
                    r"next_player\(\s*(?P<dealer>(?=[a-z_][a-z0-9_]*dealer|dealer)[a-z_][a-z0-9_]*)\s*,\s*2\s*\)"
                ),
                replace=r"\g<indent>\g<var> = 1 - \g<dealer>",
                guard="position_semantics_regex_bb",
                relevance_markers=("next_player(dealer_id, 2)", "next_player(future_dealer, 2)"),
                regex=True,
                replace_all=True,
            ),
            Patch(
                file_rel="state.py",
                search=(
                    r"(?m)^(?P<indent>\s*)(?P<var>sb|[a-z_][a-z0-9_]*_sb)\s*=\s*"
                    r"next_player\(\s*(?P<dealer>(?=[a-z_][a-z0-9_]*dealer|dealer)[a-z_][a-z0-9_]*)\s*,\s*1\s*\)"
                ),
                replace=r"\g<indent>\g<var> = \g<dealer>",
                guard="position_semantics_regex_sb",
                relevance_markers=("next_player(dealer_id, 1)", "next_player(future_dealer, 1)"),
                regex=True,
                replace_all=True,
            ),
            Patch(
                file_rel="state.py",
                search=(
                    r"(?m)^(?P<indent>\s*)(?P<var>bb|[a-z_][a-z0-9_]*_bb)\s*=\s*"
                    r"next_player\(\s*(?P<dealer>(?=[a-z_][a-z0-9_]*dealer|dealer)[a-z_][a-z0-9_]*)\s*,\s*2\s*\)"
                ),
                replace=r"\g<indent>\g<var> = 1 - \g<dealer>",
                guard="position_semantics_regex_bb",
                relevance_markers=("next_player(dealer_id, 2)", "next_player(future_dealer, 2)"),
                regex=True,
                replace_all=True,
            ),
        ],
    ),
]


def _native_national_layout(bot_dir: Path) -> bool:
    """Return True for national-native bot directories.

    Native bots have a TCP entrypoint and can legitimately omit legacy
    Botzone-era helpers. We intentionally avoid importing the bot or running the
    full protocol checker here: fix injection is called during candidate
    preparation, before later hygiene/gates have finished normalizing the bot.
    """

    return bot_dir.name.startswith("national_v") and (bot_dir / "national_bot.py").exists()


def _patch_relevant_to_native_file(content: str, patch: Patch) -> bool:
    """Decide whether a missing textual search is a real skipped fix.

    For native bot layouts, an absent legacy symbol means the patch is outside
    that bot's implementation surface. If a marker is present, the file still
    looks like the legacy area the patch protects, so skipping remains visible.
    """

    if not patch.relevance_markers:
        return True
    return any(marker in content for marker in patch.relevance_markers)


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
    A fix is "skipped" if ALL of its applicable patches were skipped (guard
    present or relevant search not found). For national-native layouts, legacy
    patches whose files/symbols are absent are not applicable and are omitted
    from both return lists.
    """
    bot_dir = Path(bot_dir)
    applied: list[str] = []
    skipped: list[str] = []
    native_layout = _native_national_layout(bot_dir)

    for fix in MANDATORY_FIXES:
        if not fix.active:
            continue

        fix_applied = False
        applicable_seen = False
        skipped_seen = False

        for patch in fix.patches:
            target = bot_dir / patch.file_rel
            if not target.exists():
                if native_layout:
                    log.debug("Fix %s not applicable: %s missing in native layout", fix.fix_id, patch.file_rel)
                else:
                    applicable_seen = True
                    skipped_seen = True
                    log.warning("Fix %s patch target missing: %s", fix.fix_id, target)
                continue

            content = target.read_text()

            # Guard check: if fixed code already present, skip. Regex
            # replace-all patches intentionally rely on search exhaustion
            # instead of a file-level guard so one fixed occurrence does not
            # mask another legacy occurrence in the same file.
            if patch.guard and patch.guard in content and not patch.replace_all:
                applicable_seen = True
                skipped_seen = True
                continue

            # Search check: if search string not found, skip
            search_found = (
                re.search(patch.search, content) is not None
                if patch.regex else patch.search in content
            )
            if not search_found:
                if native_layout and not _patch_relevant_to_native_file(content, patch):
                    log.debug(
                        "Fix %s not applicable: %s has no legacy relevance markers",
                        fix.fix_id,
                        patch.file_rel,
                    )
                    continue
                applicable_seen = True
                skipped_seen = True
                log.warning(
                    "Fix %s search not found in %s",
                    fix.fix_id, patch.file_rel,
                )
                continue

            # Apply patch
            if patch.regex:
                new_content, replacements = re.subn(
                    patch.search,
                    patch.replace,
                    content,
                    count=0 if patch.replace_all else 1,
                )
            else:
                replacements = 1
                new_content = content.replace(patch.search, patch.replace, 1)
            if new_content == content:
                applicable_seen = True
                skipped_seen = True
                log.warning("Fix %s replacement had no effect in %s", fix.fix_id, patch.file_rel)
                continue

            _locked_read_write(target, new_content)
            applicable_seen = True
            fix_applied = True
            log.info("Applied fix %s to %s (%d replacement%s)", fix.fix_id, patch.file_rel, replacements, "" if replacements == 1 else "s")

        if fix_applied:
            applied.append(fix.fix_id)
        elif applicable_seen and skipped_seen:
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

    target_v = parse_bot_version(bot_dir.name)

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
            "target_v": target_v,
            "source_v": source_v,
            "applied": applied,
            "skipped": skipped,
        },
    )
