"""Core business logic for the poker bot evolution framework.

This module re-exports everything from the focused sub-modules for backward
compatibility. All consumers can continue using `from evolution_core import X`.

Module structure:
    evolution_infra.py   — Constants, git, daemon, ratings, run_claude_query, code verification
    agent_master.py      — Master Architect + analysis/summarization helpers
    agent_workers.py     — Worker execution + retry logic
    agent_review.py      — Critic, Performance Verification, Crossover
"""

# ── Infrastructure ──
from evolution_infra import (  # noqa: F401
    # Constants
    CORE_DIR, PROJECT_ROOT, _COPY_IGNORE, PROMPTS_DIR, RESULTS_DIR, BOTS_DIR,
    EXPERIENCE_FILE, REFERENCE_DIR, GRAVEYARD_DIR, RATINGS_FILE, STATS_FILE,
    H2H_FILE, BOT_STATS_FILE, WORKER_FAILURES_FILE, PIPELINE_STATE_FILE,
    REPLAY_DIR, MATCH_HISTORY_FILE,
    MAX_ACTIVE_BOTS, DAEMON_EVAL_TIMEOUT, MIN_GAMES_FOR_EVAL, MAX_LINES_PER_FILE,
    MIN_DECISION_PASS_RATE, MIN_CROSSOVER_DECISION_RATE, MAX_WORKER_RETRIES,
    MAX_MASTER_RETRIES, MAX_CROSSOVER_RETRIES, MAX_GENESIS_RETRIES,
    WORKER_TIMEOUT, MAX_PARALLEL_WORKERS, MAX_PROMPT_CHARS,
    STAGE_ORDER, STAGE_GATE_ALLOWLIST, EVOLUTION_BRANCH, _BLOCKED_MCP_TOOLS,
    _WORKER_SEMAPHORE, daemon_proc, _daemon_lock, _atexit_registered,
    # Utility functions
    _get_worker_semaphore, _trim_to_budget, locked_file, substitute_template,
    # Pipeline checkpoints
    write_pipeline_checkpoint, read_pipeline_checkpoint, clear_pipeline_checkpoint,
    # UI
    BaseUI,
    # Bot directory
    get_bot_dir, get_logs_dir, get_active_bots, find_current_v,
    # Ratings
    load_ratings, load_daemon_stats, wait_for_daemon_eval,
    # Daemon
    _drain_stdout, start_daemon, stop_daemon, daemon_monitor_thread,
    # Git
    _git, _git_ensure_main_branch, git_has_tag, git_commit_bot, git_get_parent,
    # LLM
    run_claude_query, parse_json_output,
    # Code verification
    verify_code, check_code_size, run_smoke_test, run_decision_test_details, seed_initial_bots,
    # External re-exports
    Glicko2Player, update_rating_period, trim_experience_pool,
)

# ── Master Agent ──
from agent_master import (  # noqa: F401
    _run_master_analysis, _consolidate_experience_pool, _analyze_stagnation,
    _num_public_cards_to_street, extract_street_patterns,
    summarize_replay_for_analysis, _analyze_recent_matches,
)

# ── Worker Agent ──
from agent_workers import (  # noqa: F401
    _run_single_worker, _execute_workers,
    _record_worker_failure, _load_recent_failures,
)

# ── Review Agents ──
from agent_review import (  # noqa: F401
    _run_critic, _run_performance_verification, _run_crossover,
)
