"""Read-only catalog for source-controlled active LLM prompts.

Prompt bytes are exact generation inputs and must move through Git plus the
evaluation-contract reconciliation boundary.  The dashboard is deliberately
not a second, unaudited prompt deployment mechanism.
"""

import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = PROJECT_ROOT / "web" / "core" / "prompts"

# Explicit registry of every reachable active prompt.  Do not infer filenames:
# that previously kept a retired ``initial_prompt.md`` in the UI while omitting
# several prompts that production actually executed.
PROMPT_FILES = {
    "orchestrator": "orchestrator.md",
    "master": "master_prompt.md",
    "master_plan_audit": "master_plan_audit.md",
    "worker": "worker_prompt.md",
    "worker_profile_national_native": "worker_profile_national_native.md",
    "worker_cot_check": "worker_cot_check.md",
    "debug_worker": "debug_worker_prompt.md",
    "reviewer": "reviewer_prompt.md",
    "critic": "critic_prompt.md",
    "crossover": "crossover_prompt.md",
    "crossover_compatibility": "crossover_compatibility.md",
    "direction_auditor": "direction_auditor_prompt.md",
    "literature_probe": "literature_probe_prompt.md",
    "combined_analyst": "combined_analyst.md",
    "degeneration_diagnosis": "degeneration_diagnosis.md",
    "cycle_archivist": "cycle_archivist.md",
    "official_platform_analysis": "official_platform_analysis.md",
}
ALLOWED_PROMPTS = frozenset(PROMPT_FILES)

PROMPT_ROLES = {
    "orchestrator": "LLM Orchestrator — controls the full evolution pipeline autonomously",
    "master": "Master Architect — analyzes ratings and plans worker improvement tasks",
    "master_plan_audit": "Master Plan Auditor — checks the frozen plan contract",
    "worker": "Worker Agent — directly edits bot source code per assigned role",
    "worker_profile_national_native": "National Worker Profile — strict raw-TCP policy ABI",
    "worker_cot_check": "Worker Consistency Auditor — checks claims against the diff",
    "debug_worker": "Debug Worker — repairs one checkpoint-owned blocker",
    "reviewer": "Lead Code Reviewer — checks code quality and role boundary compliance",
    "critic": "Poker Strategy Critic — scores strategic quality 1–10",
    "crossover": "Crossover Agent — merges two elite bots into a hybrid child",
    "crossover_compatibility": "Crossover Compatibility Auditor — advisory post-publication analysis",
    "direction_auditor": "Direction Auditor — detects repetitive evolution directions before Master planning",
    "literature_probe": "Literature Probe — researches one governance-approved strategy hypothesis",
    "cycle_archivist": "Cycle Archivist — writes a content-bound archive annotation only",
    "combined_analyst": "Combined Analyst — merged stagnation detection + performance verification",
    "degeneration_diagnosis": "Degeneration Diagnostician — advisory explanation of frozen decline evidence",
    "official_platform_analysis": "Official Platform Analyst — advisory interpretation of deterministic EXE evidence",
}

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _prompt_path(name: str) -> Path:
    filename = PROMPT_FILES.get(name)
    if filename is None:
        raise KeyError(name)
    return PROMPTS_DIR / filename


def _prompt_info(name: str) -> dict:
    path = _prompt_path(name)
    if not path.exists():
        return {
            "name": name,
            "exists": False,
            "lines": 0,
            "mtime": None,
            "role": PROMPT_ROLES.get(name, ""),
            "editable": False,
            "mutation_authority": "source_control_only",
        }
    stat = path.stat()
    with open(path, "r", errors="ignore") as f:
        lines = sum(1 for _ in f)
    return {
        "name": name,
        "filename": path.name,
        "exists": True,
        "lines": lines,
        "mtime": stat.st_mtime,
        "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "role": PROMPT_ROLES.get(name, ""),
        "editable": False,
        "mutation_authority": "source_control_only",
    }


@router.get("")
async def list_prompts():
    """List all prompt files with metadata."""
    return [_prompt_info(name) for name in sorted(ALLOWED_PROMPTS)]


@router.get("/{name}", response_class=PlainTextResponse)
async def get_prompt(name: str):
    """Read a prompt file by name."""
    if name not in ALLOWED_PROMPTS:
        return PlainTextResponse(f"Unknown prompt: {name}. Allowed: {sorted(ALLOWED_PROMPTS)}", status_code=404)
    path = _prompt_path(name)
    if not path.exists():
        return PlainTextResponse(f"Prompt file not found: {path.name}", status_code=404)
    return PlainTextResponse(path.read_text(errors="replace"))
