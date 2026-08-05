"""Companion module for generation_scheduler: source-selection strategy cluster.

Holds the deterministic strategy decision, source-v loop/oscillation detection,
and crossover parent selection logic. Every intra-companion call routes through
``generation_scheduler as _gs`` so the moved symbols stay single-dispatch even
when invoked from the main module.
"""

import generation_scheduler as _gs


_COMBINED_CONFIDENCE = {"low", "medium", "high"}
_COMBINED_TRENDS = {"improving", "stagnant", "declining", "unknown"}
_COMBINED_RECOMMENDATIONS = {
    "continue", "crossover", "branch", "branch_from", "force_exploration",
}

# Minimum number of bots in the active rating pool for crossover to be
# considered.  Below this the pool is dominated by a single evolved line plus
# the first-strict bootstrap, so no genuinely different second lineage exists
# and crossover deterministically dead-loops (the bootstrap child always
# regresses).  See _pick_crossover_parents.
_MIN_CROSSOVER_POOL_SIZE = 3


def _normalize_combined_control(combined):
    """Fail closed on weak-model values even when a test/caller bypasses Pydantic."""
    if not isinstance(combined, dict):
        return None
    normalized = dict(combined)
    if normalized.get("confidence") not in _COMBINED_CONFIDENCE:
        normalized["confidence"] = "low"
    if normalized.get("trend") not in _COMBINED_TRENDS:
        normalized["trend"] = "unknown"
    if normalized.get("recommendation") not in _COMBINED_RECOMMENDATIONS:
        normalized["recommendation"] = "continue"
    for field in ("is_stagnant", "diversity_needed", "llm_failed"):
        if not isinstance(normalized.get(field), bool):
            normalized[field] = False
    return normalized


def _deterministic_fallback_source(current_v, ratings, selection_view):
    leader_v = _gs._get_unified_leader_v(ratings, selection_view)
    return leader_v if leader_v is not None else current_v


def _llm_source_eligibility(requested_v, selection_view):
    """Allow LLM source hints only inside a high-evidence leader envelope."""
    if not isinstance(selection_view, _gs.SelectionView):
        return False, "frozen_selection_view_missing"
    name = _gs.bot_name(requested_v)
    if requested_v not in selection_view.active_versions or name not in selection_view.metrics:
        return False, "source_not_active"
    leader_v = _gs._get_unified_leader_v({}, selection_view)
    if leader_v is None:
        return False, "frozen_leader_missing"
    leader_name = _gs.bot_name(leader_v)
    candidate_score = float(selection_view.selection_scores.get(name, 0.0))
    leader_score = float(selection_view.selection_scores.get(leader_name, 0.0))
    metrics = selection_view.metrics.get(name) or {}
    try:
        coverage = float(
            metrics.get(
                "opponent_coverage",
                metrics.get("h2h_coverage", 0.0),
            )
            or 0.0
        )
    except (TypeError, ValueError):
        coverage = 0.0
    confidence = str(metrics.get("strength_confidence") or "low")
    if requested_v == leader_v:
        return True, "frozen_leader"
    if coverage < 0.4:
        return False, "source_coverage_below_40pct"
    if confidence == "low":
        return False, "source_strength_confidence_low"
    if candidate_score < leader_score - 0.03:
        return False, "source_outside_leader_score_envelope"
    return True, "credible_near_leader"


def _decide_strategy(combined, current_v, ratings, *, selection_view=None):
    """Deterministic strategy selection based on combined analysis results.

    The combined analysis merges stagnation and performance data into one dict:
    - is_stagnant + confidence -> branch or crossover
    - diversity_needed -> crossover injection
    - recommendation + branch_from -> branch from specific ancestor
    """
    combined = _gs._normalize_combined_control(combined)
    if combined is None:
        return (
            "master",
            _gs._deterministic_fallback_source(current_v, ratings, selection_view),
            (),
        )

    if isinstance(selection_view, _gs.SelectionView):
        source_metrics = selection_view.metrics.get(_gs.bot_name(current_v)) or {}
        if str(source_metrics.get("strength_confidence") or "low") == "low":
            # LLM certainty cannot exceed the frozen evaluator's evidence.
            combined["confidence"] = "low"
            combined["is_stagnant"] = False
            combined["diversity_needed"] = False
            if combined.get("recommendation") in {
                "crossover", "force_exploration",
            }:
                combined["recommendation"] = "continue"

    # B-class control-flow guard: if the Combined Analyst's LLM call crashed
    # (infrastructure failure, NOT a business judgement), stagnation status is
    # UNKNOWN. The combined result's safe default claims "improving / not
    # stagnant", but that is a guess -- we must NOT act on it. In particular we
    # must avoid misfiring the crossover/stagnation branches (which assume a
    # trustworthy stagnation signal) and also avoid misreading the optimistic
    # default. Fall back to a conservative master evolution from current_v with
    # no crossover parents. A later successful, checkpoint-bound Direction
    # audit remains the only repetition constraint source.
    if combined.get("llm_failed"):
        _gs.log.warning(
            "Combined analyst reported LLM infrastructure failure -- stagnation "
            "unknown. Proceeding conservatively with master from v%d (no crossover).",
            current_v,
        )
        try:
            _gs.log_system_event(
                "pipeline.combined_analyst_infra", "warn",
                f"Stagnation analysis unavailable for v{current_v} (LLM infra error). "
                "Master proceeding, confidence=low -- no crossover triggered.",
                {"source_v": current_v},
            )
        except Exception:
            pass
        return (
            "master",
            _gs._deterministic_fallback_source(current_v, ratings, selection_view),
            (),
        )

    if combined.get("evidence_status") == "insufficient_coverage":
        return (
            "master",
            _gs._deterministic_fallback_source(current_v, ratings, selection_view),
            (),
        )

    # Source-v loop detection: if recent generations all branched from the same
    # ancestor (typically because LLM analysis anchors on a "stable" intermediate),
    # force branching from the Glicko-rated leader instead.
    _source_loop = (
        _gs._source_loop_from_history(selection_view.source_history, n=3)
        if isinstance(selection_view, _gs.SelectionView)
        else _gs._detect_source_loop(n=3)
    )
    if _source_loop:
        leader_v = _gs._get_unified_leader_v(ratings, selection_view)
        if leader_v is not None and leader_v != _source_loop:
            _gs.log.warning(
                "Source-v loop detected (last 3+ gens from v%d). "
                "Forcing source_v=%d (unified selection leader) to break the loop.",
                _source_loop, leader_v,
            )
            _gs._log_source_selection_decision(
                "source_loop_unified_leader",
                leader_v,
                current_v,
                combined,
                ratings,
                selection_view,
            )
            return "master", leader_v, ()

    # Source-v oscillation detection: if recent gens cycle among a small set
    # of ancestors, force crossover between the highest and lowest rated bots
    # from that oscillating set to break out of the cycle.
    oscillating = (
        _gs._source_oscillation_from_history(
            selection_view.source_history,
            n=8,
            max_unique=3,
        )
        if isinstance(selection_view, _gs.SelectionView)
        else _gs._detect_source_oscillation(n=8, max_unique=3)
    )
    if oscillating:
        active_versions = _gs._active_source_versions(selection_view)
        selectable_oscillating = (
            oscillating & active_versions
            if active_versions and (oscillating & active_versions)
            else set(oscillating)
        )
        # Find highest and lowest rated bots within the oscillating set, using the
        # conservative rating (r - 2*rd) so RD-inflated point estimates don't bias
        # which bots are treated as "strongest"/"weakest" crossover parents.
        osc_ratings = {}
        for sv in selectable_oscillating:
            bot_key = _gs.bot_name(sv)
            if bot_key in ratings:
                osc_ratings[sv] = ratings[bot_key].conservative_rating()
        # E2: convergence guard. If the Glicko leader (strongest active bot by
        # conservative rating) is itself inside the oscillating set, the lineage
        # has converged ONTO an elite ancestor rather than truly oscillating
        # without progress -- forcing crossover here would blow apart a winning
        # lineage (BUG2). Only force crossover when none of the recurring sources
        # is the current leader, i.e. genuine stuckness on weaker ancestors.
        leader_v = _gs._get_unified_leader_v(ratings, selection_view)
        force_oscillation_crossover = True
        if leader_v is not None and leader_v in osc_ratings:
            force_oscillation_crossover = False
            _gs.log.info(
                "Source-v oscillation suppressed: leader v%d (%.0f cons) is within the "
                "recurring set %s -- treating as convergence, not oscillation (E2).",
                leader_v, osc_ratings[leader_v], sorted(oscillating),
            )
        elif combined.get("is_stagnant") and combined.get("confidence") != "low":
            force_oscillation_crossover = False
            _gs.log.info(
                "Source-v oscillation detected but deferred to the normal stagnation "
                "crossover selector; recurring set=%s.",
                sorted(oscillating),
            )
            try:
                _gs.log_system_event(
                    "pipeline.source_oscillation_deferred",
                    "info",
                    "Source-v oscillation deferred to stagnation crossover selector",
                    {
                        "oscillating_sources": sorted(oscillating),
                        "current_v": current_v,
                        "confidence": combined.get("confidence"),
                    },
                )
            except Exception:
                pass
        else:
            breakout = _gs._pick_oscillation_breakout_source(
                oscillating,
                current_v,
                selection_view,
            )
            if breakout:
                selected_v = breakout["version"]
                _gs.log.info(
                    "Source-v oscillation broken by credible outside source v%d "
                    "(selection=%.4f, confidence=%s, osc_best=%.4f).",
                    selected_v,
                    breakout["selection_score"],
                    breakout["strength_confidence"],
                    breakout["osc_best_score"],
                )
                try:
                    _gs.log_system_event(
                        "pipeline.source_oscillation_breakout",
                        "info",
                        (
                            f"Source-v oscillation broken by v{selected_v} "
                            f"(selection={breakout['selection_score']:.4f})"
                        ),
                        {
                            "selected_source_v": selected_v,
                            "current_v": current_v,
                            "oscillating_sources": sorted(oscillating),
                            "selection_score": round(breakout["selection_score"], 4),
                            "strength_confidence": breakout["strength_confidence"],
                            "osc_best_score": round(breakout["osc_best_score"], 4),
                            "score_margin": round(breakout["score_margin"], 4),
                            "basis": breakout["basis"],
                        },
                    )
                except Exception:
                    pass
                _gs._log_source_selection_decision(
                    "source_oscillation_breakout",
                    selected_v,
                    current_v,
                    combined,
                    ratings,
                    selection_view,
                )
                return "master", selected_v, ()
        if force_oscillation_crossover and len(osc_ratings) >= 2:
            highest_v = max(osc_ratings, key=osc_ratings.get)
            lowest_v = min(osc_ratings, key=osc_ratings.get)
            if highest_v != lowest_v:
                _gs.log.warning(
                    "Source-v oscillation: forcing crossover between highest-rated v%d (%.0f cons) "
                    "and lowest-rated v%d (%.0f cons) from oscillating set %s",
                    highest_v, osc_ratings[highest_v],
                    lowest_v, osc_ratings[lowest_v],
                    sorted(oscillating),
                )
                _gs._log_crossover_decision("oscillation", highest_v, (highest_v, lowest_v),
                                        osc_ratings.get(highest_v), osc_ratings.get(lowest_v),
                                        ratings=ratings, selection_view=selection_view)
                return "crossover", highest_v, (highest_v, lowest_v)

    # Priority 1: Stagnation with high/medium confidence -> crossover
    # This is the PRIMARY escape hatch from local optima -- must fire before
    # recommended_source so stagnation always triggers diversity injection.
    if combined.get("is_stagnant") and combined.get("confidence") != "low":
        parents = _gs._pick_crossover_parents(
            ratings,
            current_v,
            selection_view=selection_view,
        )
        if parents:
            _gs._log_crossover_decision(
                "stagnation",
                parents[0],
                parents,
                ratings=ratings,
                selection_view=selection_view,
            )
            return "crossover", parents[0], parents

    # Priority 2: LLM-recommended source (only for non-stagnant systems).
    # Validate against the strict published active pool.
    rec_source = combined.get("recommended_source", "")
    if rec_source:
        rec_v = _gs._parse_branch_from(rec_source)
        if rec_v is not None and rec_v >= 1:
            # Never accept uncommitted directories or retired epoch artifacts
            # as an evolution source.
            eligible, eligibility_reason = _gs._llm_source_eligibility(
                rec_v,
                selection_view,
            )
            if eligible:
                if rec_v != current_v:
                    rationale = combined.get("source_rationale", "")
                    _gs.log.info("LLM recommended source: v%d (instead of latest v%d). %s",
                             rec_v, current_v, rationale[:200])
                _gs._log_source_selection_decision(
                    "llm_recommended_source",
                    rec_v,
                    current_v,
                    combined,
                    ratings,
                    selection_view,
                )
                return "master", rec_v, ()
            _gs._log_source_selection_rejected(
                "llm_recommended_source",
                rec_v,
                current_v,
                eligibility_reason,
                combined,
            )

    # Priority 3: Explicit branch recommendation
    if combined.get("recommendation") == "branch" and combined.get("branch_from"):
        branch_v = _gs._parse_branch_from(combined["branch_from"])
        if branch_v is not None and branch_v >= 1:
            eligible, eligibility_reason = _gs._llm_source_eligibility(
                branch_v,
                selection_view,
            )
            if eligible:
                _gs._log_source_selection_decision(
                    "branch_recommendation",
                    branch_v,
                    current_v,
                    combined,
                    ratings,
                    selection_view,
                )
                return "master", branch_v, ()
            _gs._log_source_selection_rejected(
                "branch_recommendation",
                branch_v,
                current_v,
                eligibility_reason,
                combined,
            )

    # Priority 4: Diversity injection
    if (
        combined.get("diversity_needed")
        and combined.get("confidence") in {"medium", "high"}
    ):
        parents = _gs._pick_crossover_parents(
            ratings,
            current_v,
            selection_view=selection_view,
        )
        if parents:
            _gs.log.info("Diversity injection: forcing crossover (%s, %s) to break local optimum",
                     f"v{parents[0]}", f"v{parents[1]}")
            _gs._log_crossover_decision(
                "diversity",
                parents[0],
                parents,
                ratings=ratings,
                selection_view=selection_view,
            )
            return "crossover", parents[0], parents

    # Fallback: weak/empty LLM guidance cannot override the frozen leader.
    fallback_v = _gs._deterministic_fallback_source(current_v, ratings, selection_view)
    _gs._log_source_selection_decision(
        "frozen_leader_fallback",
        fallback_v,
        current_v,
        combined,
        ratings,
        selection_view,
    )
    return "master", fallback_v, ()


def _parse_branch_from(branch_str: str) -> int | None:
    """Parse only current canonical bot labels or positive numeric versions.

    Advisory LLM output used to accept arbitrary ``*_vN`` and bare ``vN``
    aliases.  That kept retired Botzone namespaces syntactically alive in the
    source-selection path.  Active eligibility is canonical, so parsing must be
    canonical too; the later pool/tag checks remain the authority.
    """

    token = str(branch_str or "").strip()
    if token.isdecimal():
        value = int(token)
        return value if value > 0 else None
    return _gs.parse_bot_version(token)


def _read_source_v_history():
    """Return current-epoch lineage from strict immutable publications only."""
    try:
        from bot_namespace import FIRST_STRICT_POLICY_VERSION
        from evolution_infra import git_get_parent
        from national_runtime_authority import strict_published_bot_names

        versions = sorted(
            version
            for name in strict_published_bot_names()
            if (version := _gs.parse_bot_version(name)) is not None
            and version >= FIRST_STRICT_POLICY_VERSION
        )
        sources = []
        for version in versions:
            source_v = git_get_parent(version)
            if source_v is not None and int(source_v) >= FIRST_STRICT_POLICY_VERSION:
                sources.append(int(source_v))
        return sources
    except Exception:
        return []


def _detect_source_loop(n=3):
    """Check if the last n generations all used the same source_v.

    Returns the repeated source_v if a loop is detected, None otherwise.
    """
    try:
        sources = _gs._read_source_v_history()
        if not sources:
            return None
        return _gs._source_loop_from_history(sources, n=n)
    except Exception:
        pass
    return None


def _source_loop_from_history(sources, *, n=3):
    values = list(sources or [])
    recent = (
        values[-(n + 1):]
        if len(values) >= n + 1
        else values[-n:] if len(values) >= n else []
    )
    if len(recent) >= n and len(set(recent)) == 1:
        return recent[0]
    return None


def _detect_source_oscillation(n=8, max_unique=3):
    """Check if recent generations oscillate among a small set of source_v values.

    If the unique count among the last n source_v values is max_unique or fewer,
    the system is oscillating -- repeatedly switching between the same small set
    of ancestors without convergence.

    Returns the set of oscillating source_v values if detected, None otherwise.
    """
    try:
        sources = _gs._read_source_v_history()
        if not sources:
            return None
        unique_sources = _gs._source_oscillation_from_history(
            sources,
            n=n,
            max_unique=max_unique,
        )
        if unique_sources:
            _gs.log.warning("Source-v oscillation detected: last %d gens used only %d unique sources: %s",
                        min(len(sources), n), len(unique_sources), sorted(unique_sources))
            return unique_sources
    except Exception:
        pass
    return None


def _source_oscillation_from_history(sources, *, n=8, max_unique=3):
    recent = list(sources or [])[-n:]
    if len(recent) < max_unique + 1:
        return None
    unique_sources = set(recent)
    return unique_sources if len(unique_sources) <= max_unique else None


def _get_unified_leader_v(ratings, selection_view=None):
    """Return the version number of the strongest active bot for source repair.

    Prefer the confidence-discounted ``selection_score`` used by the dashboard
    and crossover/precommit mechanics. Fall back to conservative Glicko
    (r - 2*rd) if the unified snapshot is unavailable, so source-loop recovery
    still works during partial data or cache failures.
    """
    if not ratings and not isinstance(selection_view, _gs.SelectionView):
        return None
    active_versions = _gs._active_source_versions(selection_view)
    if isinstance(selection_view, _gs.SelectionView):
        eligible_bots = list(selection_view.active_bots)
        selection_scores = selection_view.selection_scores
        selection_order_keys = selection_view.order_keys
    else:
        eligible_bots = []
        selection_scores = {}
        selection_order_keys = {}
    rating_versions = {
        version for version in (_gs._parse_branch_from(name) for name in ratings)
        if version is not None
    }
    if not isinstance(selection_view, _gs.SelectionView):
        filter_by_active = bool(active_versions and (rating_versions & active_versions))
        eligible_bots = [
            name for name in ratings
            if (_gs._parse_branch_from(name) in active_versions if filter_by_active else True)
        ]
    if not eligible_bots:
        return None
    if not isinstance(selection_view, _gs.SelectionView):
        try:
            from tool_helpers import load_selection_scores
            selection_scores = load_selection_scores()
        except Exception:
            selection_scores = {}
        try:
            from tool_helpers import load_selection_order_keys
            selection_order_keys = load_selection_order_keys()
        except Exception:
            selection_order_keys = {}

    def _score(name):
        raw = selection_scores.get(name)
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
        rating = ratings.get(name)
        if rating is None:
            return float("-inf")
        try:
            return max(0.0, min(1.0, 0.5 + (rating.conservative_rating() - 1500.0) / 800.0))
        except Exception:
            return float("-inf")

    def _order_key(name):
        primary = _score(name)
        recorded = selection_order_keys.get(name)
        secondary = tuple(recorded[1:]) if recorded and float(recorded[0]) == primary else ()
        return (primary, *secondary, _gs._parse_branch_from(name) or -1)

    best_bot = max(eligible_bots, key=_order_key)
    try:
        return int(best_bot.split("_v")[1])
    except (ValueError, IndexError):
        return None


def _pick_oscillation_breakout_source(
    oscillating: set[int],
    current_v: int,
    selection_view=None,
) -> dict | None:
    """Pick a credible source outside an oscillating ancestor set.

    The oscillation backstop is supposed to break stale source loops, not erase a
    newly validated elite. Use the same confidence-discounted selection score
    exposed to the dashboard and evolution mechanics. When several credible bots
    are effectively tied for first, prefer the newest version so the system keeps
    moving forward instead of snapping back to an old historical champion.
    """
    if isinstance(selection_view, _gs.SelectionView):
        metrics = selection_view.metrics
    else:
        try:
            from tool_helpers import load_h2h_avg_winrates_with_coverage

            metrics = load_h2h_avg_winrates_with_coverage()
        except Exception:
            return None

    if not metrics:
        return None
    active_versions = _gs._active_source_versions(selection_view)
    metric_versions = {
        version for version in (_gs._parse_branch_from(name) for name in metrics)
        if version is not None
    }
    filter_by_active = bool(active_versions and (metric_versions & active_versions))

    def _score(data: dict) -> float:
        raw = data.get("selection_score", data.get("leaderboard_score", 0.0))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    osc_scores = []
    for sv in oscillating:
        osc_metrics = metrics.get(_gs.bot_name(sv))
        if osc_metrics:
            osc_scores.append(_score(osc_metrics))
    if not osc_scores:
        return None
    osc_best = max(osc_scores)

    candidates = []
    for name, data in metrics.items():
        version = _gs._parse_branch_from(name)
        if version is None or version in oscillating:
            continue
        if filter_by_active and version not in active_versions:
            continue
        confidence = data.get("strength_confidence", "low")
        if confidence == "low":
            continue
        score = _score(data)
        if score < osc_best + _gs.OSCILLATION_BREAKOUT_MIN_MARGIN:
            continue
        candidates.append({
            "version": version,
            "selection_score": score,
            "strength_confidence": confidence,
            "osc_best_score": osc_best,
            "score_margin": score - osc_best,
            "basis": data.get("rank_basis", ""),
        })

    if not candidates:
        return None

    best_score = max(c["selection_score"] for c in candidates)
    near_best = [
        c for c in candidates
        if c["selection_score"] >= best_score - _gs.OSCILLATION_BREAKOUT_SCORE_TOLERANCE
    ]
    for candidate in near_best:
        if candidate["version"] == current_v:
            return candidate
    return max(near_best, key=lambda c: (c["version"], c["selection_score"]))


def _pick_crossover_parents(
    ratings,
    current_v,
    selection_view=None,
) -> tuple | None:
    """Select parents solely from the immutable frozen strength order.

    Parent A is the strongest row in ``SelectionView``. Parent B is the
    strongest remaining row at least three versions away; when the published
    pool is too narrow, the strongest adjacent row is the deterministic
    fallback. No mutable sidecar or compatibility ledger participates.
    """
    if not isinstance(selection_view, _gs.SelectionView):
        return None
    active = list(selection_view.active_bots)
    strength = selection_view.selection_scores
    strength_order = selection_view.order_keys
    # Crossover recombines two distinct evolved lineages to escape a local
    # optimum.  It is only meaningful when the active rating pool is rich
    # enough to offer a genuinely DIFFERENT second lineage: with fewer than
    # ``_MIN_CROSSOVER_POOL_SIZE`` active bots the pool is dominated by the
    # single strongest line plus the first-strict bootstrap, so the only
    # available parent B is structurally incapable of contributing new
    # capabilities (the bootstrap is a minimal seed).  Every such crossover
    # child regresses a parent-A capability, the architecture-policy gate
    # correctly rejects it, and the generation is abandoned after exhausting
    # retries — a deterministic dead-loop that wastes the full Master+Worker
    # LLM budget each time (observed: v30-v75, ~30 abandoned generations).
    # Disable crossover until the pool grows; the system falls back to
    # single-parent Master evolution from the strongest bot, which still
    # advances the lineage.  Crossover re-enables automatically once
    # certification admits more bots into the pool.
    if len(active) < _MIN_CROSSOVER_POOL_SIZE:
        try:
            _gs.log_system_event(
                "pipeline.crossover_pool_too_small",
                "info",
                (
                    f"Crossover disabled: active rating pool has only "
                    f"{len(active)} bot(s) (< {_MIN_CROSSOVER_POOL_SIZE} "
                    "required). Falling back to single-parent Master "
                    "evolution. Crossover re-enables once more bots are "
                    "certified into the pool."
                ),
                {
                    "active_bot_count": len(active),
                    "min_pool_size": _MIN_CROSSOVER_POOL_SIZE,
                    "active_bots": list(active),
                },
            )
        except Exception:
            pass
        return None

    ranked = sorted(
        active,
        key=lambda name: tuple(strength_order.get(name, ())),
        reverse=True,
    )
    if len(ranked) < 2:
        return None

    parent_a = ranked[0]
    va = _gs.parse_bot_version(parent_a)
    if va is None:
        return None

    candidates = [
        (candidate, _gs.parse_bot_version(candidate))
        for candidate in ranked[1:]
    ]
    candidates = [
        (candidate, version)
        for candidate, version in candidates
        if version is not None
    ]
    if not candidates:
        return None
    gap_candidates = [
        (candidate, version)
        for candidate, version in candidates
        if abs(version - va) >= 3
    ]
    selection_mode = "version_gap" if gap_candidates else "adjacent_fallback"
    parent_b, vb = (gap_candidates or candidates)[0]

    try:
        _gs.log_system_event(
            "pipeline.crossover_parent_selection",
            "info",
            f"Crossover parents selected: {parent_a} x {parent_b}",
            {
                "parent_a": parent_a,
                "parent_b": parent_b,
                "parent_a_strength": round(float(strength.get(parent_a, 0.0)), 4),
                "parent_b_strength": round(float(strength.get(parent_b, 0.0)), 4),
                "version_gap": abs(vb - va),
                "selection_mode": selection_mode,
                "selection_view_digest": selection_view.digest,
            },
        )
    except Exception:
        pass
    return (va, vb)
