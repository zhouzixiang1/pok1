"""Research Governance — Ratchet-style governance for web-retrieved strategy candidates.

Implements the "bottleneck is the librarian, not the author" principle from Ratchet
(arxiv 2605.19576, "Diagnosing and Fixing a Silent Failure Mode in Self-Evolving LLM
Skill Libraries"). Unretrieved governance of LLM-authored knowledge = SkillsBench
+0.0pp vs human-curated +16.2pp; harsh retirement (N_min=20) = active HARM (-0.019).

This module governs the web_candidates pool populated by run_literature_probe (A5):
  - translation_gate: drop "plausible but untranslatable" claims (no target_fn /
    no numeric_claim) before they ever reach a worker.
  - outcome-driven retirement: a candidate that fails ≥N_min H2H trials with
    ĉ ≤ -τ is retired + its pattern is blacklisted (never re-injected).
  - bounded active-cap: pool capped at WEB_CANDIDATES_CAP to prevent retrieval
    degradation (Ratchet A7: cap=100 only raised variance, not mean).
  - cooldown: if a gen that injected a web_candidate fails precommit, disable web
    retrieval for the next COOLDOWN_GENS generations.
  - hurt-ratio kill-switch: if the active pool's hurt/(hurt+help) exceeds
    HURT_RATIO_DISABLE, disable the whole Web-Retrieval Stage.

Data model (results/web_candidates.json, list of candidate dicts):
  {id, claim, source_url, numeric_claim, target_fn, proposed_change,
   born_gen, applied_to_bot, trials, attributed_hurt, attributed_help,
   status: "active"|"retired"|"blacklisted", retired_reason}

All writes are fcntl-locked via evolution_infra helpers. Best-effort: a governance
failure must never block the evolution pipeline.
"""

import json
import os
import time
import hashlib
from pathlib import Path

from evolution_infra import (
    RESULTS_DIR,
    read_locked_json,
    write_locked_json,
    append_locked_jsonl,
    locked_file,
)
from system_log import log_system_event

# ── Ratchet-derived constants (see arxiv 2605.19576 ablations) ──────────────
WEB_CANDIDATES_CAP = 5          # active pool cap (Ratchet: bounded library)
RETIRE_N_MIN = 30               # min trials before retirement eligible
                              # (Ratchet A4: N_min=20 → -0.019 active harm; use 30)
RETIRE_TAU = -0.10              # ĉ ≤ -τ triggers retirement
COOLDOWN_GENS = 2               # disable web retrieval for this many gens after a FAIL
HURT_RATIO_DISABLE = 0.40       # active-pool hurt ratio kill-switch

WEB_CANDIDATES_FILE = RESULTS_DIR / "web_candidates.json"
WEB_BLACKLIST_FILE = RESULTS_DIR / "web_blacklist.jsonl"
GOVERNANCE_STATE_FILE = RESULTS_DIR / "research_governance_state.json"


# ── Pool I/O ────────────────────────────────────────────────────────────────
def _load_pool():
    """Load the web_candidates pool (list of dicts). Missing/corrupt → empty list."""
    try:
        data = read_locked_json(WEB_CANDIDATES_FILE, default=None)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("candidates"), list):
            return data["candidates"]
    except Exception:
        pass
    return []


def _save_pool(pool):
    """Persist the pool (best-effort, fcntl-locked)."""
    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        write_locked_json(WEB_CANDIDATES_FILE, {"candidates": pool, "cap": WEB_CANDIDATES_CAP})
    except Exception as e:  # never block the pipeline
        log_system_event("research_governance.save_failed", "warn",
                         f"web_candidates save failed: {e}", {})


def _load_state():
    try:
        data = read_locked_json(GOVERNANCE_STATE_FILE, default=None)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_state(state):
    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        write_locked_json(GOVERNANCE_STATE_FILE, state)
    except Exception:
        pass


# ── Core governance primitives ──────────────────────────────────────────────
def score_candidate(c):
    """ĉ = (attributed_help - attributed_hurt) / max(trials, 1). Higher is better."""
    trials = max(int(c.get("trials", 0)), 1)
    help_n = int(c.get("attributed_help", 0))
    hurt_n = int(c.get("attributed_hurt", 0))
    return (help_n - hurt_n) / trials


def translation_gate(candidate):
    """Ratchet translation gate (DeepEvolve anti-pollution): a web-derived claim is
    only admissible if it can be translated into a CONCRETE code change — i.e. it
    names a target function AND a numeric claim (sizing/freq/equity threshold).
    Vague "play more aggressively" claims are dropped before they reach a worker.
    Returns True if admissible."""
    target_fn = str(candidate.get("target_fn", "")).strip()
    numeric_claim = str(candidate.get("numeric_claim", "")).strip()
    return bool(target_fn and numeric_claim)


def _pattern_fingerprint(candidate):
    """Stable fingerprint for blacklist matching (claim + target_fn)."""
    raw = (str(candidate.get("target_fn", "")) + "|" + str(candidate.get("claim", ""))).lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def is_pattern_blacklisted(candidate):
    """True if this candidate's (target_fn, claim) pattern was previously retired
    into the blacklist. Prevents reward-hacking via repeated bad retrieval."""
    fp = _pattern_fingerprint(candidate)
    if not WEB_BLACKLIST_FILE.exists():
        return False
    try:
        with open(WEB_BLACKLIST_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("fingerprint") == fp:
                    return True
    except Exception:
        return False
    return False


def add_candidate(candidate):
    """Add a web-derived candidate to the pool after gating. Returns the assigned id
    or None if rejected (translation gate fail / blacklisted / pool full of better
    candidates). Enforces the bounded active-cap by evicting the lowest-ĉ active
    candidate when at cap."""
    if not translation_gate(candidate):
        log_system_event("research_governance.translation_gate_drop", "info",
                         "web candidate dropped (no target_fn/numeric_claim)",
                         {"claim": str(candidate.get("claim", ""))[:120]})
        return None
    if is_pattern_blacklisted(candidate):
        log_system_event("research_governance.blacklist_block", "info",
                         "web candidate dropped (pattern blacklisted)",
                         {"target_fn": candidate.get("target_fn")})
        return None

    pool = _load_pool()
    active = [c for c in pool if c.get("status") == "active"]
    candidate = dict(candidate)
    cid = candidate.get("id") or f"wc_{int(time.time())}_{len(pool)}"
    candidate["id"] = cid
    candidate.setdefault("born_gen", None)
    candidate.setdefault("applied_to_bot", None)
    candidate.setdefault("trials", 0)
    candidate.setdefault("attributed_hurt", 0)
    candidate.setdefault("attributed_help", 0)
    candidate["status"] = "active"
    candidate["fingerprint"] = _pattern_fingerprint(candidate)

    if len(active) >= WEB_CANDIDATES_CAP:
        # Evict the lowest-ĉ active candidate (Ratchet bounded cap).
        active.sort(key=lambda c: score_candidate(c))
        victim = active[0]
        victim["status"] = "retired"
        victim["retired_reason"] = "evicted_by_cap"
        log_system_event("research_governance.cap_evict", "info",
                         f"web candidate {victim.get('id')} evicted (cap={WEB_CANDIDATES_CAP})",
                         {"evicted_id": victim.get("id"), "score": score_candidate(victim)})

    pool.append(candidate)
    _save_pool(pool)
    log_system_event("research_governance.candidate_added", "info",
                     f"web candidate {cid} added to pool",
                     {"id": cid, "target_fn": candidate.get("target_fn"),
                      "source_url": candidate.get("source_url", "")[:120]})
    return cid


# ── Trigger gate (used by run_literature_probe A5) ──────────────────────────
def should_trigger_web_retrieval(next_v):
    """Decide whether run_literature_probe should run this generation. False when:
      - globally disabled (hurt-ratio kill-switch), or
      - within cooldown after a web-injected gen failed precommit.
    `next_v` is the upcoming generation number."""
    state = _load_state()
    if state.get("disabled", False):
        return False
    cooldown_until = state.get("cooldown_until_gen")
    if cooldown_until is not None and next_v is not None and next_v <= cooldown_until:
        return False
    return True


def trigger_cooldown(gen, reason="precommit_fail"):
    """Disable web retrieval for the next COOLDOWN_GENS after a web-injected gen fails."""
    state = _load_state()
    cooldown_until = (gen if gen is not None else 0) + COOLDOWN_GENS
    state["cooldown_until_gen"] = cooldown_until
    state["cooldown_reason"] = reason
    _save_state(state)
    log_system_event("research_governance.cooldown", "warn",
                     f"web retrieval disabled until gen {cooldown_until} ({reason})",
                     {"cooldown_until_gen": cooldown_until, "reason": reason})


def disable_web_retrieval_stage(reason="hurt_ratio_exceeded"):
    """Kill-switch: disable the Web-Retrieval Stage entirely (hurt ratio too high)."""
    state = _load_state()
    state["disabled"] = True
    state["disabled_reason"] = reason
    _save_state(state)
    log_system_event("research_governance.stage_disabled", "error",
                     f"Web-Retrieval Stage DISABLED ({reason})", {"reason": reason})


def hurt_ratio():
    """hurt / (hurt + help) over the active pool. 0.0 if no verdicts yet."""
    pool = _load_pool()
    active = [c for c in pool if c.get("status") == "active"]
    help_n = sum(int(c.get("attributed_help", 0)) for c in active)
    hurt_n = sum(int(c.get("attributed_hurt", 0)) for c in active)
    total = help_n + hurt_n
    return hurt_n / total if total > 0 else 0.0


def check_and_maybe_disable():
    """If the active-pool hurt ratio exceeds HURT_RATIO_DISABLE, disable the stage."""
    ratio = hurt_ratio()
    if ratio > HURT_RATIO_DISABLE:
        disable_web_retrieval_stage(reason=f"hurt_ratio={ratio:.2f}>{HURT_RATIO_DISABLE}")
        return True
    return False


# ── Outcome-driven retirement (Ratchet core) ────────────────────────────────
def record_outcome(candidate_id, won=None, hurt_verdict=None, n_games=0,
                   bot_version=None):
    """Feed a daemon/precommit outcome back into a candidate's attribution counters.

    won: True/False/None — did the bot (that used this candidate) beat the target
         opponent / pass precommit vs parent.
    hurt_verdict: optional critic attribution ('helped'|'hurt'|'neutral'|'inapplicable').
    n_games: H2H game count contributing to this observation.

    After updating, checks retirement eligibility (trials ≥ RETIRE_N_MIN AND
    ĉ ≤ RETIRE_TAU) and the hurt-ratio kill-switch."""
    if not candidate_id:
        return
    pool = _load_pool()
    updated = False
    for c in pool:
        if c.get("id") != candidate_id:
            continue
        c["trials"] = int(c.get("trials", 0)) + max(int(n_games or 0), 1 if n_games == 0 else 0)
        if won is True:
            c["attributed_help"] = int(c.get("attributed_help", 0)) + 1
        elif won is False:
            c["attributed_hurt"] = int(c.get("attributed_hurt", 0)) + 1
        if hurt_verdict == "helped":
            c["attributed_help"] = int(c.get("attributed_help", 0)) + 1
        elif hurt_verdict == "hurt":
            c["attributed_hurt"] = int(c.get("attributed_hurt", 0)) + 1
        if bot_version:
            c["applied_to_bot"] = bot_version
        updated = True
        # Retirement check
        if (c.get("status") == "active"
                and int(c.get("trials", 0)) >= RETIRE_N_MIN
                and score_candidate(c) <= RETIRE_TAU):
            _retire_candidate(c, reason=f"low_score_after_{c['trials']}_trials")
        break
    if updated:
        _save_pool(pool)
        check_and_maybe_disable()


def _retire_candidate(candidate, reason):
    """Mark a candidate retired + blacklist its pattern (permanent re-injection ban)."""
    candidate["status"] = "retired"
    candidate["retired_reason"] = reason
    try:
        append_locked_jsonl(WEB_BLACKLIST_FILE, {
            "fingerprint": candidate.get("fingerprint") or _pattern_fingerprint(candidate),
            "id": candidate.get("id"),
            "target_fn": candidate.get("target_fn"),
            "claim": str(candidate.get("claim", ""))[:200],
            "reason": reason,
            "score": score_candidate(candidate),
            "ts": int(time.time()),
        })
    except Exception:
        pass
    log_system_event("research_governance.retire", "warn",
                     f"web candidate {candidate.get('id')} RETIRED + blacklisted ({reason})",
                     {"id": candidate.get("id"), "reason": reason,
                      "score": score_candidate(candidate)})


def active_candidates_for_prompt():
    """Return the active candidate list (sorted by ĵ desc) for master_prompt injection.
    Used by run_master to expose web-derived hypotheses to the LLM."""
    pool = _load_pool()
    active = [c for c in pool if c.get("status") == "active"]
    active.sort(key=score_candidate, reverse=True)
    return active


def record_precommit_outcome(bot_version, passed, next_v=None):
    """Hook called from run_precommit_eval after a bot (that may have incorporated
    web-derived candidates) is evaluated vs parent. Feeds the pass/fail outcome back
    into every active candidate applied to this bot version, and triggers a cooldown
    if a web-injected gen FAILED precommit (Ratchet anti-pollution: stop injecting
    noise after a failure). Best-effort, never blocks precommit."""
    if bot_version is None:
        return
    try:
        pool = _load_pool()
        touched = [c for c in pool if c.get("status") == "active"
                   and c.get("applied_to_bot") == bot_version]
        if not touched:
            return
        for c in touched:
            record_outcome(c.get("id"), won=bool(passed), n_games=1, bot_version=bot_version)
        if not passed:
            trigger_cooldown(next_v, reason=f"precommit_fail_v{bot_version}")
    except Exception as e:
        log_system_event("research_governance.precommit_hook_error", "warn",
                         f"record_precommit_outcome failed: {e}", {})
