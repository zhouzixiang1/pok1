"""Static capability contract for national-native bot architecture.

This gate is intentionally separate from protocol legality.  The validator and
official EXE decide whether a bot may act on the wire; this module tells the
evolution pipeline whether a candidate is using the national-native runtime
model well: bounded decision work, reusable precomputation, persistent
match-memory, and clean diagnostics.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any


def _read_python_sources(bot_dir: str | Path) -> dict[str, str]:
    root = Path(bot_dir)
    sources: dict[str, str] = {}
    for path in sorted(root.glob("*.py")):
        try:
            sources[path.name] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return sources


def _contains(text: str, *patterns: str) -> bool:
    lower = text.lower()
    return any(pattern.lower() in lower for pattern in patterns)


def _regex(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) is not None


def _check(name: str, passed: bool, severity: str, evidence: str, guidance: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "severity": severity,
        "evidence": evidence,
        "guidance": guidance,
    }


def evaluate_national_capabilities(bot_dir: str | Path) -> dict[str, Any]:
    """Evaluate architecture-level national-native capabilities.

    Required checks are wire-safety basics that must stay true.  Advisory checks
    are the evolution direction requested for future generations: exploit the
    60-second budget with precomputed/cached facts and persistent match memory
    rather than slow per-action recomputation.
    """
    sources = _read_python_sources(bot_dir)
    joined = "\n".join(sources.values())
    national_bot = sources.get("national_bot.py", "")
    strategy_text = "\n".join(
        text for name, text in sources.items()
        if name in {"strategy.py", "postflop.py", "simulation.py", "opponent.py", "state.py", "constants.py"}
    )

    checks = [
        _check(
            "official_safe_wire_send",
            "_send_wire_action" in national_bot and "POK_OFFICIAL_ACTION_DELAY" in national_bot,
            "required",
            "national_bot.py preserves the official action send helper and throttle",
            "Send formal EXE actions only through _send_wire_action and keep POK_OFFICIAL_ACTION_DELAY near 0.30 by default.",
        ),
        _check(
            "clean_diagnostics_channel",
            ("--log" in national_bot or "POK_TRACE_DECISIONS" in national_bot)
            and not _regex(national_bot, r"(?m)^\s*print\s*\("),
            "required",
            "diagnostics are available without stdout pollution",
            "Write communication/decision diagnostics to --log or stderr; never print diagnostics to stdout in the native TCP entry.",
        ),
        _check(
            "decision_time_budget_visible",
            _contains(joined, "time.monotonic", "perf_counter", "elapsed", "duration_ms", "decision_ms")
            and _contains(strategy_text or joined, "max_", "limit", "cap", "samples", "budget", "deadline"),
            "advisory",
            "decision paths expose timing or bounded-work markers",
            "Make per-action work bounded and observable; add deadline-aware fallback before increasing simulation/search.",
        ),
        _check(
            "precompute_lookup_path",
            _contains(strategy_text, "precompute", "lookup", "bucket", "cache", "memo", "table")
            and not _regex(strategy_text, r"def\s+get_action[\s\S]{0,2000}(precompute|build_.*table|lookup\s*=\s*\{)"),
            "advisory",
            "pure poker facts can be reused instead of rebuilt inside get_action",
            "Move pure card/range/texture computations into bounded module/startup lookup tables or immutable caches.",
        ),
        _check(
            "persistent_match_memory",
            _contains(national_bot, "_requests", "_history", "_showdowns")
            and _contains(national_bot, "earnchips", "oppo_hands"),
            "advisory",
            "native client keeps match-level request/history/showdown state",
            "Keep hand state separate from match state; preserve match-level opponent summaries across the 70 hands.",
        ),
        _check(
            "incremental_opponent_model",
            _contains(joined, "opponenttracker", "incremental", "update_opponent", "record_opponent", "match_profile")
            and _contains(strategy_text, "opponent", "opp_"),
            "advisory",
            "opponent model appears incrementally updated and consumed by strategy",
            "Prefer an OpponentTracker-style object over rebuilding the whole model from full request history every action.",
        ),
    ]

    required_failures = [item for item in checks if item["severity"] == "required" and not item["passed"]]
    advisory_warnings = [item for item in checks if item["severity"] == "advisory" and not item["passed"]]
    return {
        "schema_version": 1,
        "bot_dir": str(Path(bot_dir)),
        "ok": not required_failures,
        "required_failures": required_failures,
        "advisory_warnings": advisory_warnings,
        "checks": checks,
    }
