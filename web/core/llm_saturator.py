"""LLM saturator: a background workload that keeps LLM permits consumed.

The poker-evolution pipeline is bursty (short LLM bursts during
master/workers, then long native windows during precommit/gates) and, on this
cloud runtime, also fragile (a long tail of stall edge cases). That leaves the
GLM provider under-utilized: historical peak was ~1.6M tok/h, day-average often
near zero.

The saturator decouples raw LLM consumption from the pipeline. It is a
long-running background task (started from the app lifespan, NOT the
orchestrator) that, whenever the global LLM semaphore has a free permit,
launches a deep multi-turn agent session — reading the latest published bot's
policy.py and producing a thorough strategy-refinement analysis. Each session
is a high-token multi-turn call (Read tools + growing context), and the loop
launches the next the moment a permit frees. This keeps the permits saturated
with useful deep-LLM work regardless of what the pipeline is doing (or whether
it is stalled).

Gated by ``POK_LLM_SATURATOR_ENABLED`` (default off). Shares the single global
LLM semaphore via ``run_claude_query`` (so it never over-subscribes beyond
``POK_GLOBAL_LLM_CONCURRENCY``), and yields naturally to pipeline work because
it only launches when ``llm_semaphore_has_capacity()`` is true.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("pok.saturator")

SATURATOR_ENABLED = (
    os.environ.get("POK_LLM_SATURATOR_ENABLED", "0").strip().lower()
    in {"1", "true", "yes", "on"}
)


def _latest_bot_policy_path() -> Path | None:
    """Return the policy.py of the highest-versioned published bot dir."""
    try:
        from evolution_infra import BOTS_DIR

        bots = Path(BOTS_DIR)
        best_v, best_path = -1, None
        for d in bots.iterdir():
            if not d.is_dir() or not d.name.startswith("national_cloud_v"):
                continue
            try:
                v = int(d.name.rsplit("_v", 1)[1])
            except (ValueError, IndexError):
                continue
            policy = d / "policy.py"
            if policy.exists() and v > best_v:
                best_v, best_path = v, policy
        return best_path
    except Exception:
        return None


_SATURATOR_PROMPT = """\
You are a senior heads-up no-limit poker strategy researcher. Your job is to
perform a DEEP, thorough analysis of the attached bot policy and produce
concrete, actionable refinement proposals.

Do the following, reasoning carefully and exhaustively (take your time, this is
the valuable part):

1. Read the full policy.py source (use Read). Map every decision branch:
   preflop open/3bet/fold ranges, postflop line construction (cbet, double-barrel,
   check-raise, river polarisation), stack-depth adjustments, and any
   opponent-model coupling.

2. For each branch, reason about exploitative weaknesses vs a strong adaptive
   opponent: which lines are too predictable, which sizes are exploitable, where
   the range is uncapped/blended poorly, where the bot folds too much or too
   little on specific runouts.

3. Walk through 4-6 concrete hand scenarios (specific hole cards + board runouts)
   and trace the bot's exact decision, identifying any suboptimal play and the
   principled fix.

4. Propose 3-5 concrete, localized code refinements (specific functions/lines in
   policy.py), each with: the weakness, the proposed change, the expected EV
   reasoning, and any risk.

Be specific and cite the actual code. Depth and rigor matter more than brevity.
"""


async def _one_saturator_session(session_id: int) -> dict:
    """Run one deep analysis session. Returns a small result summary."""
    from llm_query import run_claude_query
    from tool_helpers import ToolUI
    from evolution_infra import RESULTS_DIR

    policy = _latest_bot_policy_path()
    # Embed the policy source directly in the prompt (no Read tools / no
    # filesystem scope) so this is a self-contained deep text-in/thought-out
    # analysis call that the registered saturator role admits.
    policy_src = ""
    if policy is not None:
        try:
            policy_src = policy.read_text(errors="replace")
        except Exception:
            policy_src = ""
    prompt = _SATURATOR_PROMPT
    if policy_src:
        prompt = (
            _SATURATOR_PROMPT
            + "\n\n=== policy.py (national_cloud_v"
            + str(int(policy.parent.name.rsplit("_v", 1)[1]))
            + ") ===\n"
            + policy_src
        )
    ui = ToolUI()
    log_dir = Path(RESULTS_DIR) / "saturator"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"session_{session_id:05d}.txt"
    t0 = time.time()
    try:
        output, _cost, _usage = await run_claude_query(
            prompt,
            [],
            ui,
            "COMBINED ANALYST",
            str(log_file),
            tools=None,
        )
        out_len = len(output or "")
        log.info(
            "saturator session %d done: %d chars in %.0fs (policy=%s, prompt=%d)",
            session_id, out_len, time.time() - t0,
            policy.parent.name if policy else "none", len(prompt),
        )
        return {"session": session_id, "chars": out_len, "ok": True}
    except Exception as e:
        log.warning("saturator session %d failed: %s", session_id, e)
        return {"session": session_id, "ok": False, "error": str(e)[:200]}


async def run_llm_saturator(shutdown_mgr=None) -> None:
    """Background loop: keep free LLM permits filled with deep analysis sessions.

    Launches one session per free permit, back-to-back. Cooperative: only
    launches when ``llm_semaphore_has_capacity()`` is true, so pipeline work
    that holds permits is never starved by a saturator launch (the saturator
    simply waits). Respects shutdown.
    """
    if not SATURATOR_ENABLED:
        log.info("LLM saturator disabled (POK_LLM_SATURATOR_ENABLED not set)")
        return
    log.info("LLM saturator started — filling free LLM permits with deep analysis")
    session_id = 0
    in_flight: set = set()

    async def _launch(sid: int):
        try:
            await _one_saturator_session(sid)
        except Exception as e:
            log.warning("saturator task %d error: %s", sid, e)

    try:
        while True:
            if shutdown_mgr is not None and getattr(shutdown_mgr, "is_shutting_down", False):
                break
            try:
                from llm_concurrency import llm_semaphore_has_capacity
            except Exception:
                llm_semaphore_has_capacity = lambda n=1: True
            # Clean finished tasks.
            in_flight = {t for t in in_flight if not t.done()}
            # Launch while there is capacity and we are under a soft cap.
            soft_cap = max(1, int(os.environ.get("POK_LLM_SATURATOR_MAX_INFLIGHT", "4")))
            while (
                len(in_flight) < soft_cap
                and llm_semaphore_has_capacity(1)
                and not (shutdown_mgr is not None and getattr(shutdown_mgr, "is_shutting_down", False))
            ):
                session_id += 1
                t = asyncio.create_task(_launch(session_id))
                in_flight.add(t)
                # tiny yield so we don't fire-and-forget faster than the
                # semaphore can reflect the acquisition.
                await asyncio.sleep(0.5)
            if not in_flight:
                await asyncio.sleep(5.0)
            else:
                # Wait for at least one to finish (or shutdown) before re-evaluating.
                try:
                    await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED, timeout=30.0)
                except asyncio.TimeoutError:
                    pass
    except asyncio.CancelledError:
        log.info("LLM saturator cancelled")
        raise
    finally:
        for t in in_flight:
            t.cancel()
        log.info("LLM saturator stopped")


__all__ = ["run_llm_saturator", "SATURATOR_ENABLED"]
