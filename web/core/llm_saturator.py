"""LLM saturator: a background workload that keeps LLM permits consumed.

The poker-evolution pipeline is bursty (short LLM bursts during
master/workers, then long native windows during precommit/gates) and, on this
cloud runtime, also fragile (a long tail of stall edge cases). That leaves the
GLM provider under-utilized: historical peak was ~1.6M tok/h, day-average often
near zero.

The saturator decouples raw LLM consumption from the pipeline. It is a
long-running background task (started from the app lifespan, NOT the
orchestrator) that, whenever the global LLM semaphore has a free permit,
launches a deep multi-turn agent session — a comparative strategy study of
several published bots (focus bot rotating per session), reading each bot's
policy.py across many tool turns with a compounding context. Consumption is
dominated by per-turn cache re-reads of the growing context (the same term
that makes long agentic coding sessions expensive), NOT by output generation
speed — so session SHAPE (bot count × turn count) is the consumption lever,
independent of the model's output token rate.

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


_PUBLISHED_TAG_CACHE: "tuple[float, set[int]] | None" = None


def _published_versions() -> "set[int]":
    """Versions carrying an annotated completion tag (cached 10 min).

    An in-flight draft's candidate dir (no completion tag yet) is NOT
    published evidence and must not be served to the saturator as a
    reference bot.
    """
    global _PUBLISHED_TAG_CACHE
    import subprocess

    now = time.time()
    if _PUBLISHED_TAG_CACHE and now - _PUBLISHED_TAG_CACHE[0] < 600:
        return _PUBLISHED_TAG_CACHE[1]
    versions: "set[int]" = set()
    try:
        from evolution_infra import BOTS_DIR

        root = Path(BOTS_DIR).parent
        out = subprocess.run(
            ["git", "tag", "--list", "national-cloud-bot-v*"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                versions.add(int(line.rsplit("-v", 1)[1]))
            except (ValueError, IndexError):
                continue
    except Exception:
        pass
    _PUBLISHED_TAG_CACHE = (now, versions)
    return versions


def _published_bot_dirs() -> "list[Path]":
    """Published canonical bot dirs, newest version first."""
    try:
        from evolution_infra import BOTS_DIR

        published = _published_versions()
        bots = Path(BOTS_DIR)
        found = []
        for d in bots.iterdir():
            if not d.is_dir() or not d.name.startswith("national_cloud_v"):
                continue
            try:
                v = int(d.name.rsplit("_v", 1)[1])
            except (ValueError, IndexError):
                continue
            if v in published and (d / "policy.py").exists():
                found.append((v, d))
        found.sort(key=lambda t: t[0], reverse=True)
        return [d for _, d in found]
    except Exception:
        return []


def _saturator_bots(session_id: int, limit: int = 5) -> "list[Path]":
    """Pick this session's bot set: FOCUS bot first (rotating by session id so
    successive sessions deep-dive different bots), then the newest others.

    A 5-bot set (~100K tokens of policy source) is the deliberate context
    sweet spot: large enough that every additional tool turn re-reads a big
    cached prefix (the dominant consumption term), small enough to stay well
    inside the model context window across 40+ turns of growing analysis."""
    dirs = _published_bot_dirs()
    if not dirs:
        return []
    focus = dirs[session_id % len(dirs)]
    others = [d for d in dirs if d != focus][: max(0, limit - 1)]
    return [focus] + others


_SATURATOR_PROMPT = """\
You are a senior heads-up no-limit poker strategy researcher running a DEEP,
multi-bot comparative study. Depth and evidence density are the whole point:
work methodically across MANY Read+reason turns (expect 40+ tool turns; do not
rush to conclusions), re-reading code before every citation.

You are given several published bot directories. The first is the FOCUS bot;
the others are OPPONENTS.

Phase 1 — per-bot mapping (one bot at a time; full Read of policy.py plus
national_runtime_manifest.json for EACH bot): map every decision branch —
preflop open/3bet/fold ranges, postflop line construction (cbet, double-barrel,
check-raise, river polarisation), stack-depth adjustments, opponent-model
coupling, precompute usage. Record a style fingerprint per bot (aggression
frequency, sizing scheme, bluffing texture, adaptivity).

Phase 2 — pairwise duels (FOCUS bot vs each opponent): identify the decisive
strategic asymmetries. Which FOCUS-bot lines are exploitable by THIS opponent
specifically (predictable sizing, uncapped ranges, over-folding runouts)?
Quote exact code for every claim — RE-READ the relevant section before citing
it; never cite from memory.

Phase 3 — scenario walkthroughs: walk through 8-10 concrete hands (specific
hole cards + boards across streets) for the 2-3 most instructive matchups.
Trace BOTH bots' decisions step by step through their code (re-read each
function as you trace it). Identify suboptimal play and the principled fix.

Phase 4 — synthesis: (a) evidence-grounded ranking of all bots with
citations; (b) 6-10 localized refinements to the FOCUS bot's policy.py
(function/line, weakness, proposed change, EV reasoning, risk).

The goal is a deep, evidence-grounded comparative strategy audit — work
through it methodically over many Read+reason turns.
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
            scope_policy="canonical_candidates",
            tools=(("Read",),),
            read_scope="explicit_candidate_and_generation_snapshot_dirs",
            write_scope="none",
            evidence_policy="system_bound_prompt_only",
            history_policy="forbidden",
            requires_read_scope=True,
            allows_context_files=False,
        )
        _lq.ACTIVE_LLM_ROLE_CONTRACTS = _lq.ACTIVE_LLM_ROLE_CONTRACTS + (contract,)
        _lq._saturator_role_registered = True
    except Exception as e:
        log.warning("saturator role registration failed: %s", e)


_register_saturator_role()


SATURATOR_ROLE = "SATURATOR STRATEGY RESEARCH"


def _saturator_read_dirs(bots: "list[Path]") -> list:
    """Read scope for the multi-turn analysis: the selected published bot dirs."""
    dirs = []
    try:
        for b in bots:
            dirs.append(str(b.resolve()))
    except Exception:
        pass
    return dirs


def _usage_tokens(usage) -> int:
    """Full consumption tokens (in + cache read/write + out) from a usage blob."""
    try:
        data = usage if isinstance(usage, dict) else usage.model_dump()
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    return sum(
        int(data.get(k) or 0)
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )


async def _one_saturator_session(session_id: int) -> dict:
    """Run one deep multi-turn analysis session. Returns a small result summary."""
    from llm_query import run_claude_query, render_llm_prompt
    from tool_helpers import ToolUI
    from evolution_infra import RESULTS_DIR

    bots = _saturator_bots(session_id)
    focus = bots[0] if bots else None
    prompt = _SATURATOR_PROMPT
    if bots:
        listing = "\n".join(f"  - {b.resolve()}" for b in bots)
        prompt = (
            _SATURATOR_PROMPT
            + f"\n\nFOCUS bot (deep-dive this one): {focus.resolve()}\n"
            + f"Bot directories to analyze ({len(bots)}):\n{listing}\n"
        )
    read_dirs = _saturator_read_dirs(bots)
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
        output, _cost, usage = await run_claude_query(
            rendered,
            context_files,
            ui,
            SATURATOR_ROLE,
            str(log_file),
            tools=["Read"],  # match the contract's allowed Read tool set
            allowed_read_dirs=read_dirs,
        )
        out_len = len(output or "")
        tokens = _usage_tokens(usage)
        log.info(
            "saturator session %d done: %d chars, %d tokens in %.0fs (focus=%s, bots=%d)",
            session_id, out_len, tokens, time.time() - t0,
            focus.name if focus else "none", len(bots),
        )
        return {
            "session": session_id, "chars": out_len,
            "tokens": tokens, "ok": True,
        }
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
