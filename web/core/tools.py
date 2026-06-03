"""MCP tools for the Evolution Orchestrator Agent.

This module re-exports all tools and registers the MCP server for backward
compatibility. Tools are organized into:

    tool_helpers.py   — Shared helpers, UI injection, checkpoint gates
    tool_pipeline.py  — Core pipeline tools (Master → Workers → Review → Commit)
    tool_status.py    — Status queries, daemon control, bot management

Tools split into two groups:
    - MCP tools: registered for the LLM Orchestrator session (~15 tools)
    - Code-layer tools: called directly by generation_scheduler.py (not in MCP)
"""

import sys
from pathlib import Path

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_agent_sdk import create_sdk_mcp_server

from tool_helpers import inject_ui  # noqa: F401

from tool_pipeline import (  # noqa: F401
    run_master,
    execute_workers,
    run_quality_gates,
    prepare_next_gen,
    run_direction_audit,
    run_review,
    run_critic,
    run_precommit_eval,
    run_inline_eval,
    commit_bot,
    run_archivist,
    run_crossover,
)

from tool_status import (  # noqa: F401
    get_status,
    get_bot_info,
    get_match_history,
    run_match_analysis,
    run_performance_verification,
    start_eval_daemon,
    stop_eval_daemon,
    wait_for_eval,
    reap_weakest,
    trim_experience,
    seed_initial_bots_tool,
    consolidate_experience,
    analyze_stagnation,
    get_h2h,
    get_bot_stats,
    cleanup_incomplete,
    abandon_generation,
    diagnose_environment,
)

# ── MCP tools — available to the LLM Orchestrator session (~15 tools) ──

mcp_tools = [
    # Pipeline tools
    run_master,
    execute_workers,
    run_quality_gates,
    run_review,
    run_critic,
    run_precommit_eval,
    run_crossover,
    prepare_next_gen,
    run_direction_audit,
    commit_bot,
    run_archivist,
    # Query tools
    get_bot_info,
    get_match_history,
    get_h2h,
    get_bot_stats,
]

evolution_server = create_sdk_mcp_server(
    name="evolution",
    version="1.0.0",
    tools=mcp_tools,
)

# all_tools includes MCP tools + code-layer tools (used by /api/control/tool/* HTTP endpoints)
all_tools = mcp_tools + [
    get_status,
    run_match_analysis,
    run_performance_verification,
    start_eval_daemon,
    stop_eval_daemon,
    wait_for_eval,
    run_inline_eval,
    reap_weakest,
    trim_experience,
    seed_initial_bots_tool,
    consolidate_experience,
    analyze_stagnation,
    cleanup_incomplete,
    abandon_generation,
    diagnose_environment,
]
