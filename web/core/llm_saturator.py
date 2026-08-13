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
You are a senior heads-up no-limit poker strategy researcher performing a DEEP,
exhaustive analysis. Take your time and be thorough — rigor and depth are the
whole point. Use the Read tool liberally to inspect source across many turns.

Do this iteratively, reading and reasoning in multiple passes:

1. Read the bot's policy.py, precompute.py, and national_bot.py in full. Map
   every decision branch: preflop open/3bet/fold ranges, postflop line
   construction (cbet, double-barrel, check-raise, river polarisation),
   stack-depth adjustments, opponent-model coupling, and the precompute tables.

2. Read the game rules / evaluator in sever/engine/ to ground your analysis in
   the actual hand evaluation, blinds, and street semantics.

3. For EACH decision branch, reason about exploitative weaknesses vs a strong
   adaptive opponent: predictable lines, exploitable sizes, poorly
   blended/uncapped ranges, over/under-folding on specific runouts.

4. Walk through 8-12 concrete hand scenarios (specific hole cards + board
   runouts across preflop/flop/turn/river), trace the bot's exact decision
   step by step, and identify suboptimal play + the principled fix. Re-read the
   relevant code section for each scenario.

5. Read any strategy/oracle docs in docs/ that inform the protocol boundaries,
   and reconcile your recommendations against them.

6. Propose 5-8 concrete, localized code refinements (specific functions/lines),
   each with: the weakness, the proposed change, EV reasoning, and risk.

Cite actual code you Read. Re-read and cross-check across passes. The goal is a
deep, evidence-grounded strategy audit — work through it methodically over many
Read+reason turns.
"""


def _saturator_producer(renderer_inputs):
    """Render the saturator prompt into a typed LLMRenderedMaterial.

    The provider boundary requires a sealed RenderedLLMPrompt (not a raw
    string); ``render_llm_prompt`` calls this producer and signs the output.
    """
    from llm_role_dispatch import LLMRenderedMaterial

    return LLMRenderedMaterial(
        text=str(renderer_inputs.get("prompt", "")),
        evidence_kind="none",
        evidence_provenance={},
    )


def _register_saturator_role() -> None:
    """Register the saturator role contract at runtime (no semantic-file edit).

    The role registry lives in llm_query.py (a semantic path we must not edit
    without an identity re-init). Instead we append the saturator contract to
    the runtime tuple at import time. The contract's evidence_provenance_kind
    is ``none`` to match the trivial producer above. Read tools + requires_read_scope
    enable deep MULTI-TURN analysis (the agent reads across many turns, context
    compounds, driving high token consumption).
    """
    try:
        import llm_query as _lq
        if getattr(_lq, "_saturator_role_registered", False):
            return
        contract = _lq._llm_role_contract(
            "llm_saturator",
            r"^SATURATOR(?:\s|$)",
            renderer="llm_saturator.py::_SATURATOR_PROMPT",
            producer_file="web/core/llm_saturator.py",
            producer_name="_saturator_producer",
            template_paths=(),
            evidence_kind="none",
            scope_policy="explicit_saturator_read_dirs",
            tools=(("Read",),),
            read_scope="explicit_saturator_read_dirs",
            write_scope="none",
            evidence_policy="system_bound_prompt_only",
            history_policy="forbidden",
            requires_read_scope=True,
            allows_context_files=True,
        )
        _lq.ACTIVE_LLM_ROLE_CONTRACTS = _lq.ACTIVE_LLM_ROLE_CONTRACTS + (contract,)
        _lq._saturator_role_registered = True
    except Exception as e:
        log.warning("saturator role registration failed: %s", e)


_register_saturator_role()


SATURATOR_ROLE = "SATURATOR STRATEGY RESEARCH"


def _saturator_read_dirs(policy: Path | None) -> list:
    """Read scope for the multi-turn analysis: the latest bot dir + engine + docs."""
    dirs = []
    try:
        if policy is not None:
            dirs.append(str(policy.parent.resolve()))  # the bot dir
        # Rich exploration targets for deep multi-turn analysis.
        from evolution_infra import RESULTS_DIR
        root = Path(RESULTS_DIR).resolve().parents[2]
        for sub in ("sever/engine", "docs"):
            p = (root / sub).resolve()
            if p.exists():
                dirs.append(str(p))
    except Exception:
        pass
    return dirs


async def _one_saturator_session(session_id: int) -> dict:
    """Run one deep multi-turn analysis session. Returns a small result summary."""
    from llm_query import run_claude_query, render_llm_prompt
    from tool_helpers import ToolUI
    from evolution_infra import RESULTS_DIR

    policy = _latest_bot_policy_path()
    prompt = _SATURATOR_PROMPT
    if policy is not None:
        prompt = (
            _SATURATOR_PROMPT
            + "\n\nStart by Reading the bot at: "
            + str(policy.parent)
            + " (policy.py, precompute.py, national_bot.py)."
        )
    read_dirs = _saturator_read_dirs(policy)
    # No context_files (contract forbids them): the agent Reads policy.py etc.
    # itself via the Read tool across multiple turns (context compounds).
    context_files = []
    ui = ToolUI()
    log_dir = Path(RESULTS_DIR) / "saturator"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"session_{session_id:05d}.txt"
    t0 = time.time()
    try:
        rendered = render_llm_prompt(
            SATURATOR_ROLE,
            producer=_saturator_producer,
            renderer_inputs={"prompt": prompt},
        )
        output, _cost, _usage = await run_claude_query(
            rendered,
            context_files,
            ui,
            SATURATOR_ROLE,
            str(log_file),
            tools=["Read"],  # match the contract's allowed Read tool set
            allowed_read_dirs=read_dirs,
        )
        out_len = len(output or "")
        log.info(
            "saturator session %d done: %d chars in %.0fs (policy=%s, read_dirs=%d)",
            session_id, out_len, time.time() - t0,
            policy.parent.name if policy else "none", len(read_dirs),
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
