"""Code verification tools: compile check, file size, smoke test, decision tests."""

import os
import shutil
import subprocess
import sys

from evolution_infra import (
    CORE_DIR, REFERENCE_DIR, RESULTS_DIR,
    MAX_LINES_PER_FILE, MAX_LINES_HELPER, MAX_LINES_HARD_CAP,
    LINE_GROWTH_BUDGET, CORE_STRATEGY_FILES, _COPY_IGNORE,
    get_bot_dir,
)


def _count_file_lines(path):
    """Count lines in a file."""
    with open(path) as fh:
        return sum(1 for _ in fh)


def _get_adaptive_limit(filename, base_limit, source_dir=None):
    """Compute adaptive line limit for a file.

    If source_dir is provided and the source file exists, allow growth from
    the source file's size. The limit is:
        max(base_limit, source_lines * (1 + LINE_GROWTH_BUDGET))
    capped at MAX_LINES_HARD_CAP.

    Without source_dir, returns base_limit (backward compatible).
    """
    if source_dir is None:
        return base_limit

    source_path = os.path.join(source_dir, filename)
    if not os.path.exists(source_path):
        return base_limit

    source_lines = _count_file_lines(source_path)
    adaptive = max(base_limit, int(source_lines * (1 + LINE_GROWTH_BUDGET)))
    return min(adaptive, MAX_LINES_HARD_CAP)


def verify_code(directory, target_files=None):
    """Verify Python files compile. When target_files is given, only check those
    files instead of walking the entire directory — avoids false compile errors
    from other workers mid-edit in parallel mode."""
    errors = []
    if target_files:
        for tf in target_files:
            path = os.path.join(directory, tf) if not os.path.isabs(tf) else tf
            if os.path.exists(path) and path.endswith(".py"):
                proc = subprocess.run([sys.executable, "-m", "py_compile", path], capture_output=True, text=True)
                if proc.returncode != 0:
                    errors.append(proc.stderr.strip())
    else:
        # Original behavior unchanged - walk entire directory
        for root, _, files in os.walk(directory):
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(root, f)
                    proc = subprocess.run([sys.executable, "-m", "py_compile", path], capture_output=True, text=True)
                    if proc.returncode != 0:
                        errors.append(proc.stderr.strip())
    return errors


def check_code_size(directory, max_lines_per_file=None, source_dir=None):
    """Check single-file LOC limits (excluding backup files). Returns (total, oversized_files).

    Uses tiered limits: CORE_STRATEGY_FILES (strategy.py, postflop.py) get
    MAX_LINES_PER_FILE (2000), all others get MAX_LINES_HELPER (1500).

    When source_dir is provided, applies adaptive limits based on the source
    bot's file sizes plus a growth budget (LINE_GROWTH_BUDGET = 15%).
    All limits are capped at MAX_LINES_HARD_CAP (2500).
    """
    oversized_files = []
    total = 0
    for root, _, files in os.walk(directory):
        for f in files:
            if f.endswith(".py") and "backup" not in f:
                path = os.path.join(root, f)
                lines = _count_file_lines(path)
                total += lines

                # Compute limit: base → adaptive (if source_dir) → override (if max_lines_per_file)
                base_limit = MAX_LINES_PER_FILE if f in CORE_STRATEGY_FILES else MAX_LINES_HELPER
                limit = _get_adaptive_limit(f, base_limit, source_dir)

                # Explicit override wins (backward compatibility)
                if max_lines_per_file is not None:
                    limit = max_lines_per_file

                if lines > limit:
                    oversized_files.append((f, lines, limit))
    return total, oversized_files


def run_smoke_test(directory):
    main_path = os.path.join(directory, "main.py")
    if not os.path.exists(main_path):
        return ["main.py not found!"]
    proc = subprocess.run(
        [sys.executable, str(CORE_DIR / "smoke_tester.py"), main_path],
        capture_output=True, text=True
    )
    if proc.returncode != 0:
        return [proc.stderr.strip() or proc.stdout.strip()]
    return []


def run_decision_test_details(directory, extra_scenarios=None):
    """Run standard decision scenarios. Returns detailed gate results."""
    main_path = os.path.join(directory, "main.py")
    if not os.path.exists(main_path):
        return {
            "pass_rate": 0.0,
            "passed": 0,
            "total": 0,
            "critical_passed": 0,
            "critical_total": 0,
            "critical_failures": [{"id": "main.py", "details": "main.py not found"}],
            "failures": [{"id": "main.py", "severity": "critical", "details": "main.py not found"}],
            "scenarios": [],
        }
    from decision_tester import run_decision_tests_detail as _run_detail
    return _run_detail(main_path, verbose=False, extra_scenarios=extra_scenarios)


def seed_initial_bots(ui):
    """Seed claude_v1 through claude_v6 with bot1 through bot6 if they don't exist."""
    seeded = False
    for i in range(1, 7):
        target_dir = get_bot_dir(i)
        source_dir = REFERENCE_DIR / f"bot{i}"
        if not target_dir.exists() and source_dir.exists():
            ui.log_history(f"Seeding claude_v{i} from reference bot{i}...", "info")
            shutil.copytree(source_dir, target_dir, ignore=_COPY_IGNORE)
            # Apply known fixes to seeded bot
            from fix_injection import apply_known_fixes, log_fix_application
            applied, skipped = apply_known_fixes(target_dir)
            if applied or skipped:
                log_fix_application(applied, skipped, target_dir, i)
            (target_dir / ".completed").touch()
            seeded = True
    return seeded
