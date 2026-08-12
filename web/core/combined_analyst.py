"""Generation analyst over one immutable national-native evidence snapshot."""

import json
import hashlib
import logging
import os
from pathlib import Path

log = logging.getLogger('pok.analyst')

# Max prepare-phase combined-analyst attempts. Default 1: logs (2026-08-10)
# proved retrying did not recover GLM 0-TextBlock storms — 33 dispatches for a
# single v105 call still collapsed to safe_default, each ~100s stream piled on
# the LLM semaphore. One attempt + fast safe-default unblocks prepare instead.
_ANALYST_MAX_ATTEMPTS = max(1, int(os.environ.get("POK_COMBINED_ANALYST_MAX_ATTEMPTS", "1")))

CAUSAL_ANALYSIS_UNKNOWN = (
    "unknown: no content-bound artifact/diff digest is present in this frozen "
    "analyst envelope"
)

from bot_namespace import FIRST_STRICT_POLICY_VERSION, bot_name, parse_bot_version
from llm_availability import LLMAvailabilityBlocked
from strength_order import match_score
from evolution_infra import (
    run_claude_query, parse_json_output, substitute_template,
    get_logs_dir,
    PROMPTS_DIR,
    Glicko2Player,
)


def _render_combined_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    if not isinstance(inputs, dict) or set(inputs) != {
        "source_v", "frozen_bundle",
    }:
        raise ValueError("Combined Analyst renderer input contract mismatch")
    frozen_bundle = inputs["frozen_bundle"]
    if not isinstance(frozen_bundle, dict):
        raise ValueError("Combined Analyst frozen bundle must be an object")
    template_values = frozen_bundle.get("rendered_view")
    if not isinstance(template_values, dict):
        raise ValueError("Combined Analyst template values must be an object")
    expected_values = {
        "bot_name", "opp_eval", "opp_total", "opp_coverage", "rd_warning",
        "top_bots", "generation_trend", "lineage", "daemon_history",
        "bot_stats", "h2h_results",
    }
    if set(template_values) != expected_values:
        raise ValueError("Combined Analyst template value contract mismatch")
    template = (
        Path(__file__).resolve().parent / "prompts" / "combined_analyst.md"
    ).read_text(encoding="utf-8")
    text = substitute_template(
        template,
        {key: str(value) for key, value in template_values.items()},
    )
    frozen_bundle_digest = hashlib.sha256(
        json.dumps(
            frozen_bundle,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="immutable_generation_evaluation_bundle",
        evidence_provenance={
            "source_v": int(inputs["source_v"]),
            "frozen_bundle_digest": frozen_bundle_digest,
        },
    )


def _statistical_stagnation_check(source_v, ratings, history_snaps=None):
    """Pure-code stagnation check using sliding window on rating history.

    Returns (is_stagnant, confidence, trend_delta) or None if insufficient data.
    - trend_delta < 5: stagnant (high confidence)
    - trend_delta > 20: improving (high confidence)
    - trend_delta in [5, 20]: ambiguous — needs LLM analysis
    """
    if history_snaps is None:
        return None

    source_bot_name = bot_name(source_v)
    # E1: use daemon_run_id-isolated snapshots (cross-run period jumps would
    # otherwise corrupt the recent-vs-previous delta).
    snaps = list(history_snaps)[-10:]

    # Extract bot's rating from last 10 periods
    recent_ratings = []
    for snap in snaps:
        try:
            bot_rating = snap.get("ratings", {}).get(source_bot_name, {})
            r = bot_rating.get("r")
            if r is not None:
                recent_ratings.append(r)
        except (AttributeError, KeyError):
            continue

    if len(recent_ratings) < 6:
        return None  # Not enough data for comparison

    # Compare recent 3 vs previous 3
    recent_avg = sum(recent_ratings[-3:]) / 3
    previous_avg = sum(recent_ratings[-6:-3]) / 3
    delta = recent_avg - previous_avg

    bot_rd = ratings.get(source_bot_name, Glicko2Player()).rd if ratings else 350

    # High RD means rating is unreliable — statistical check is not trustworthy
    if bot_rd > 150:
        return None  # Let LLM decide

    if abs(delta) < 5:
        return (True, "high", delta)  # Flat — stagnant
    elif delta > 20:
        return (False, "high", delta)  # Clearly improving
    elif delta < -20:
        return (True, "high", delta)  # Clearly declining — needs intervention
    else:
        return None  # Ambiguous — needs LLM


async def _run_combined_analysis(
    source_v,
    active_bots,
    ratings,
    ui,
    h2h_data: dict | None = None,
    bot_stats_data: dict | None = None,
    selection_rows_data: list[dict] | None = None,
    rating_history_data: list[dict] | None = None,
):
    """Combined stagnation + performance analysis in a single LLM call.

    Returns a dict with unified fields:
    - is_stagnant, confidence, trend, diversity_needed, recommendation,
      branch_from, verified_improvements, persistent_weaknesses, reason, suggestion
    Returns a safe default on failure.
    """
    from tool_helpers import strength_row_to_analysis_view

    safe_default = {
        "is_stagnant": False,
        "confidence": "low",
        "trend": "unknown",
        "diversity_needed": False,
        "diversity_reason": None,
        "recommendation": "continue",
        "branch_from": None,
        "verified_improvements": [],
        "persistent_weaknesses": [],
        "reason": "Analysis failed, defaulting to continue",
        "suggestion": None,
        "recommended_source": "",
        "source_rationale": "",
        "causal_analysis": CAUSAL_ANALYSIS_UNKNOWN,
        "evidence_status": "analysis_unavailable",
        # llm_failed defaults to False: the safe_default only becomes an infra
        # signal when set by the LLM-crash except branch (below). The statistical
        # pre-check / coverage-shortfall paths are real business judgements, not
        # infra failures, so they correctly leave llm_failed=False.
        "llm_failed": False,
    }

    if any(
        value is None
        for value in (
            h2h_data,
            bot_stats_data,
            selection_rows_data,
            rating_history_data,
        )
    ):
        safe_default["reason"] = (
            "Missing generation-scoped frozen evidence; live result files are "
            "not an allowed fallback."
        )
        safe_default["evidence_status"] = "missing_frozen_evidence"
        return safe_default

    frozen_h2h_data = dict(h2h_data)
    rows = [dict(row) for row in selection_rows_data]
    h2h_winrates = {
        row["name"]: (
            row.get("h2h_avg_wr")
            if row.get("h2h_avg_wr") is not None
            else row.get("win_rate")
            if row.get("win_rate") is not None
            else row.get("leaderboard_score", 0.5)
        )
        for row in rows
    }
    strength_scores = {
        row["name"]: row.get("selection_score", row.get("leaderboard_score", 0.5))
        for row in rows
    }
    from strength_order import strength_order_key
    selection_order_keys = {row["name"]: strength_order_key(row) for row in rows}
    coverage_data = {
        row["name"]: strength_row_to_analysis_view(row) for row in rows
    }

    # ── Data sufficiency check ──
    source_bot_name = bot_name(source_v)
    bot_cov = coverage_data.get(source_bot_name, {})
    opp_coverage = bot_cov.get("opponent_coverage", 1.0)
    opp_eval = bot_cov.get("opponents_evaluated", 0)
    opp_total = bot_cov.get("opponents_total", 0)

    if opp_coverage < 0.8:
        safe_default["reason"] = (
            f"Insufficient opponent coverage: {opp_eval}/{opp_total} ({opp_coverage:.0%}). "
            "Need more daemon evaluation games before analysis is reliable."
        )
        safe_default["evidence_status"] = "insufficient_coverage"
        return safe_default

    # ── Statistical pre-check — skip LLM if trend is clear-cut ──
    stat_result = _statistical_stagnation_check(
        source_v,
        ratings,
        history_snaps=rating_history_data,
    )
    if stat_result is not None:
        is_stagnant, confidence, delta = stat_result
        if confidence == "high":
            trend = "stagnant" if is_stagnant else ("improving" if delta > 0 else "declining")
            return {
                "is_stagnant": is_stagnant,
                "confidence": confidence,
                "trend": trend,
                "diversity_needed": is_stagnant,
                "diversity_reason": f"Rating delta={delta:.1f} over last 6 periods — {'flat' if is_stagnant else 'clear trend'}" if is_stagnant else None,
                "recommendation": "crossover" if is_stagnant else "continue",
                "branch_from": None,
                "verified_improvements": [],
                "persistent_weaknesses": [],
                "reason": f"Statistical check: rating delta={delta:.1f} (recent 3 vs previous 3 periods). {'Stagnation detected' if is_stagnant else 'Improvement trend'}.",
                "suggestion": None,
                "recommended_source": "",
                "source_rationale": "Statistical pre-check did not evaluate source recommendation — LLM call was skipped.",
                "causal_analysis": CAUSAL_ANALYSIS_UNKNOWN,
                "evidence_status": "sufficient_statistical_precheck",
            }

    # ── Build context data (merged from both old analysts) ──

    # Generation trend is restricted to the frozen active pool. Historical
    # tags that were reaped have no row in this cycle and must not be rendered
    # as fake score=0 regressions.
    gen_trend_lines = []
    try:
        active_versions = []
        for name in active_bots:
            try:
                active_versions.append((int(str(name).rsplit("_v", 1)[1]), str(name)))
            except (IndexError, TypeError, ValueError):
                continue
        for v, v_name in sorted(active_versions)[-8:]:
            cov = coverage_data.get(v_name)
            if not isinstance(cov, dict):
                continue
            wr = cov.get("h2h_avg_wr", h2h_winrates.get(v_name))
            score = cov.get("leaderboard_score", strength_scores.get(v_name))
            if wr is None or score is None:
                continue
            cov_pct = cov.get("opponent_coverage", 0.0)
            gen_trend_lines.append(
                f"  v{v}: score={score:.4f}, h2h_avg_wr={wr:.2%} "
                f"(coverage={cov_pct:.0%})"
            )
    except Exception as e:
        log.debug('Generation trend computation failed: %s', e)
    lineage_lines = []
    try:
        from evolution_infra import git_get_parent
        strict_versions = sorted({
            version
            for name in active_bots
            if (version := parse_bot_version(str(name))) is not None
            and version >= FIRST_STRICT_POLICY_VERSION
        })
        for check_v in strict_versions[-6:]:
            parent = git_get_parent(check_v)
            if parent is not None and int(parent) >= FIRST_STRICT_POLICY_VERSION:
                lineage_lines.append(f"  v{check_v} ← parent: v{parent}")
    except Exception as e:
        log.debug('Lineage analysis failed: %s', e)
    history_ctx = ""
    recent_history = list(rating_history_data)[-10:]
    if recent_history:
        for snap in recent_history:
            try:
                wr_data = snap.get("win_rates", {})
                wrs = [(k, v["h2h_avg_wr"]) for k, v in wr_data.items() if v.get("h2h_avg_wr") is not None]
                if wrs:
                    wrs.sort(key=lambda x: x[1], reverse=True)
                    top3 = ", ".join(f"{k}={v:.3f}" for k, v in wrs[:3])
                    history_ctx += f"  Period {snap['period']}: {top3}\n"
                else:
                    top = max(p["r"] for p in snap["ratings"].values())
                    history_ctx += f"  Period {snap['period']}: top_r={top:.0f}\n"
            except (AttributeError, KeyError, ValueError):
                continue

    sorted_bots = sorted(
        active_bots,
        key=lambda b: selection_order_keys.get(b, (strength_scores.get(b, 0.0),)),
        reverse=True,
    )[:5]
    top_bots_lines = []
    for b in sorted_bots:
        p = ratings.get(b, Glicko2Player())
        wr = h2h_winrates.get(b, 0.0)
        score = strength_scores.get(b, 0.0)
        cov_info = coverage_data.get(b, {})
        cov_pct = cov_info.get("opponent_coverage", 1.0)
        cov_tag = f" [LOW COVERAGE {cov_pct:.0%}]" if cov_pct < 0.8 else ""
        top_bots_lines.append(f"  {b}: score={score:.4f}, h2h_avg_wr={wr:.2%} (r={p.r:.0f} rd={p.rd:.0f}){cov_tag}")

    # Bot stats
    bot_stats_line = "  No stats available"
    bs = bot_stats_data.get(source_bot_name, {})
    g = bs.get("games", 0)
    wr = bs.get("win_rate", 0.0)
    if g > 0:
        bot_stats_line = f"  {source_bot_name}: {wr:.0%} overall ({g} games)"

    # H2H per-opponent
    h2h_lines = []
    active_bot_set = set(active_bots)
    if frozen_h2h_data:
        try:
            analyzed_h2h = frozen_h2h_data
            for k, v in analyzed_h2h.items():
                parts = k.split(" vs ")
                if len(parts) != 2:
                    continue
                a_name, b_name = parts
                if source_bot_name not in (a_name, b_name):
                    continue
                opponent = b_name if source_bot_name == a_name else a_name
                if opponent not in active_bot_set:
                    continue
                bot_w = v.get("a_wins", 0) if source_bot_name == a_name else v.get("b_wins", 0)
                opp_w = v.get("b_wins", 0) if source_bot_name == a_name else v.get("a_wins", 0)
                draws = v.get("draws", 0)
                total = v.get("games", bot_w + opp_w + draws)
                wr = match_score(bot_w, draws, total)
                if wr is not None:
                    tag = " STRENGTH" if wr > 0.60 else " WEAKNESS" if wr < 0.40 else ""
                    h2h_lines.append((wr, f"  vs {opponent}: {bot_w}W-{opp_w}L-{draws}D ({wr:.0%}){tag}"))
            h2h_lines.sort(key=lambda x: x[0])
        except Exception as e:
            log.debug('H2H per-opponent analysis failed: %s', e)

    # RD warning
    bot_rd = ratings.get(source_bot_name, Glicko2Player()).rd if ratings else 350
    rd_warning = ""
    if bot_rd > 200:
        rd_warning = (
            f"IMPORTANT: rd={bot_rd:.0f} (>200) — rating is VERY uncertain. "
            "Trend analysis is unreliable. Treat any 'trend' with extreme skepticism."
        )
    elif bot_rd > 100:
        rd_warning = (
            f"NOTE: rd={bot_rd:.0f} (>100) — rating is moderately uncertain. "
            "Be cautious about interpreting small changes as meaningful trends."
        )

    # ── Build and run prompt ──
    template_file = (
        Path(__file__).resolve().parent / "prompts" / "combined_analyst.md"
    )
    if not template_file.exists():
        return safe_default

    template_values = {
        "bot_name": source_bot_name,
        "opp_eval": str(opp_eval),
        "opp_total": str(opp_total),
        "opp_coverage": f"{opp_coverage:.0%}",
        "rd_warning": rd_warning,
        "top_bots": "\n".join(top_bots_lines),
        "generation_trend": "\n".join(gen_trend_lines) if gen_trend_lines else "  No generation trend data",
        "lineage": "\n".join(lineage_lines) if lineage_lines else "  No lineage data",
        "daemon_history": history_ctx if history_ctx else "  No daemon history",
        "bot_stats": bot_stats_line,
        "h2h_results": "\n".join(l for _, l in h2h_lines) if h2h_lines else "  No H2H data",
    }

    log_file = get_logs_dir(source_v) / "combined_analysis.txt"
    for attempt in range(_ANALYST_MAX_ATTEMPTS):
        try:
            from llm_query import render_llm_prompt

            frozen_bundle_receipt = {
                "active_bots": list(active_bots),
                "h2h": frozen_h2h_data,
                "bot_stats": bot_stats_data,
                "selection_rows": rows,
                "rating_history": list(rating_history_data),
                "ratings": {
                    str(name): {
                        "r": float(getattr(player, "r", 1500.0)),
                        "rd": float(getattr(player, "rd", 350.0)),
                    }
                    for name, player in sorted((ratings or {}).items())
                },
                "rendered_view": template_values,
            }
            rendered_prompt = render_llm_prompt(
                "COMBINED ANALYST",
                producer=_render_combined_provider_prompt,
                renderer_inputs={
                    "source_v": int(source_v),
                    "frozen_bundle": frozen_bundle_receipt,
                },
            )
            output, _, _ = await run_claude_query(
                rendered_prompt, [], ui, "COMBINED ANALYST", log_file,
                tools=[],
            )
            from llm_query import parse_json_output_with_mode
            result, failure_mode = parse_json_output_with_mode(output)
            if result:
                from output_schema import validate_agent_output
                result, errors = validate_agent_output("combined_analyst", result)
                if errors:
                    ui.log_history(f"Combined analyst validation issues: {'; '.join(errors[:3])}", "warn")
                    continue
                # Ensure all expected fields exist
                result.setdefault("is_stagnant", False)
                result.setdefault("confidence", "low")
                result.setdefault("trend", "stagnant")
                result.setdefault("diversity_needed", result.get("is_stagnant", False))
                result.setdefault("diversity_reason", None)
                result.setdefault("recommendation", "continue")
                result.setdefault("branch_from", None)
                result.setdefault("verified_improvements", [])
                result.setdefault("persistent_weaknesses", [])
                result.setdefault("reason", "")
                result.setdefault("suggestion", None)
                result.setdefault("recommended_source", "")
                result.setdefault("source_rationale", "")
                # The current frozen bundle contains outcome/rating rows, not a
                # content-bound artifact pair or policy diff.  Never accept an
                # LLM's plausible-sounding code attribution without those
                # identities; score movement may be described, but code cause
                # remains unknown.
                result["causal_analysis"] = CAUSAL_ANALYSIS_UNKNOWN
                result["evidence_status"] = "sufficient_llm_analysis"
                return result
            ui.log_history(f"Combined analyst returned empty (attempt {attempt+1}/{_ANALYST_MAX_ATTEMPTS}, mode={locals().get('failure_mode', 'UNKNOWN')}), retrying...", "warn")
        except LLMAvailabilityBlocked:
            # Provider availability (incl. 429 quota exhaustion) is attempt-
            # neutral.  Re-raise immediately so the rate_limiter pause / quota
            # reset window is honored — never burn retries or sleep through it.
            raise
        except Exception as e:
            from llm_failure import is_llm_infra_error
            if is_llm_infra_error(e):
                ui.log_history(
                    f"Combined analyst LLM infrastructure error (NOT a business judgement): {e} "
                    f"(attempt {attempt+1}/{_ANALYST_MAX_ATTEMPTS})",
                    "warn",
                )
                # Mark the safe default so _decide_strategy (generation_scheduler)
                # treats stagnation as unknown and proceeds conservatively with
                # master (never crossover) rather than misreading a crash as
                # "improving / not stagnant".
                safe_default["llm_failed"] = True
            else:
                ui.log_history(f"Combined analyst failed: {e} (attempt {attempt+1}/{_ANALYST_MAX_ATTEMPTS})", "warn")
        # No inter-attempt sleep.  Logs (2026-08-10) proved the former
        # ``sleep(30 * (attempt + 1))`` was pure waste: GLM 0-TextBlock storms
        # did not recover after the backoff (33 dispatches for one v105 call
        # still ended in safe_default), and 429 quota is already handled by the
        # LLMAvailabilityBlocked re-raise above.  Failing fast onto the safe
        # default unblocks the prepare phase instead of queuing behind a storm.

    # If the last attempt crashed as an infra error, safe_default already carries
    # llm_failed=True. Otherwise this is the no-valid-output-after-retries path,
    # which previously collapsed silently into a business-style safe_default
    # (llm_failed=False). Emit a classifiable parse-collapse event so the
    # repeated empty-output failure is visible, and mark the default.
    if not safe_default.get("llm_failed"):
        _fm = locals().get("failure_mode", "EXCEPTION")
        try:
            from event_bus import warn
            warn("pipeline.combined_analyst_parse_failed",
                 f"Combined analyst v{source_v} parse failed after {_ANALYST_MAX_ATTEMPTS} attempts (mode={_fm}); "
                 "returning safe default (recommendation=continue)",
                 source_v=source_v, failure_mode=_fm)
        except Exception:
            pass
        safe_default["parse_failed"] = True
    return safe_default
