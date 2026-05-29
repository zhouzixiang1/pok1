"""Prompt editor endpoints — read/write LLM prompt files."""

import os
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = PROJECT_ROOT / "web" / "core" / "prompts"

# Allowed prompt names (no path traversal possible)
ALLOWED_PROMPTS = {
    "orchestrator",
    "master",
    "worker",
    "reviewer",
    "critic",
    "crossover",
    "initial",
}

PROMPT_ROLES = {
    "orchestrator": "LLM Orchestrator — controls the full evolution pipeline autonomously",
    "master": "Master Architect — analyzes ratings and plans worker improvement tasks",
    "worker": "Worker Agent — directly edits bot source code per assigned role",
    "reviewer": "Lead Code Reviewer — checks code quality and role boundary compliance",
    "critic": "Poker Strategy Critic — scores strategic quality 1–10",
    "crossover": "Crossover Agent — merges two elite bots into a hybrid child",
    "initial": "Initial Bot Generator — creates the first-generation bot from scratch",
}

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _prompt_path(name: str) -> Path:
    return PROMPTS_DIR / f"{name}_prompt.md" if name != "orchestrator" else PROMPTS_DIR / "orchestrator.md"


def _prompt_info(name: str) -> dict:
    path = _prompt_path(name)
    if not path.exists():
        return {"name": name, "exists": False, "lines": 0, "mtime": None, "role": PROMPT_ROLES.get(name, "")}
    stat = path.stat()
    lines = sum(1 for _ in open(path, "r", errors="ignore"))
    return {
        "name": name,
        "filename": path.name,
        "exists": True,
        "lines": lines,
        "mtime": stat.st_mtime,
        "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "role": PROMPT_ROLES.get(name, ""),
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


class PromptUpdateRequest(BaseModel):
    content: str


@router.put("/{name}")
async def update_prompt(name: str, req: PromptUpdateRequest):
    """Write a prompt file. Changes take effect on the next LLM call."""
    if name not in ALLOWED_PROMPTS:
        return {"error": f"Unknown prompt: {name}"}
    path = _prompt_path(name)
    try:
        path.write_text(req.content, encoding="utf-8")
        return {
            "saved": True,
            "name": name,
            "filename": path.name,
            "lines": req.content.count("\n") + 1,
        }
    except Exception as e:
        return {"error": str(e)}


@router.post("/{name}/reset")
async def reset_prompt(name: str):
    """Reset a prompt file to the last git-committed version."""
    if name not in ALLOWED_PROMPTS:
        return {"error": f"Unknown prompt: {name}"}
    path = _prompt_path(name)
    rel_path = path.relative_to(PROJECT_ROOT)
    try:
        result = subprocess.run(
            ["git", "checkout", "HEAD", "--", str(rel_path)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        if result.returncode == 0:
            info = _prompt_info(name)
            return {"reset": True, "name": name, **info}
        else:
            return {"error": result.stderr.strip() or "git checkout failed"}
    except Exception as e:
        return {"error": str(e)}
