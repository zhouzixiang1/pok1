"""Master Architect agent: plans worker tasks for the next evolution generation.

Analysis helpers (stagnation, direction audit, replay, experience, archivist)
live in their own modules. This module keeps the core Master and match analysis.
"""

import json
import time
from pathlib import Path

from bot_namespace import bot_name, bot_relpath
from evolution_infra import (
    run_claude_query, parse_json_output, substitute_template,
    locked_file, get_logs_dir, load_ratings, get_active_bots,
    _trim_to_budget, RESULTS_DIR, PROMPTS_DIR,
    MATCH_HISTORY_FILE, REPLAY_DIR,
    MAX_MASTER_RETRIES,
    get_bot_dir, MAX_LINES_HARD_CAP, CORE_STRATEGY_FILES,
)

from replay_analysis import summarize_replay_for_analysis  # noqa: F401 — re-exported via evolution_core
from output_schema import master_plan_executable_contract_text


# C-class sentinel: returned by _analyze_recent_matches /
# _run_performance_verification when their LLM call hit an infrastructure
# error (ClaudeSDKError / timeout / connection). Detected here so the Master
# prompt surfaces "analysis unavailable due to LLM failure" rather than the
# misleading "No data available" (which would imply the daemon hadn't run).
LLM_INFRA_SENTINEL = "[LLM_INFRA_ERROR: analysis unavailable]"
LLM_INFRA_SENTINEL_MSG = (
    "⚠ Analysis unavailable: the LLM analyst crashed with an infrastructure "
    "error (NOT a business judgement). Treat conclusions in this section as "
    "missing rather than negative — the daemon data still exists, only the "
    "LLM interpretation failed."
)


class MasterInfrastructureError(RuntimeError):
    """The Master role produced no plan because its LLM transport failed."""

    def __init__(self, source_v, next_v, prompt_digest, issue):
        self.source_v = source_v
        self.next_v = next_v
        self.prompt_digest = prompt_digest
        self.issue = str(issue)[:500]
        super().__init__(self.issue)


_MASTER_PROPOSAL_DIRECTIONS = (
    (
        "mechanism",
        "Propose one structural mechanism that replaces a reachable parent behavior; "
        "threshold-only tuning is invalid.",
    ),
    (
        "counterfactual",
        "Start from one falsifiable counterfactual/control and design the smallest "
        "reachable mechanism that could make it pass.",
    ),
    (
        "compute_memory",
        "Explore bounded precomputation, anytime decision work, or persistent match "
        "memory only when the injected policy/evidence makes that axis eligible.",
    ),
)


def _validated_master_proposal(output: str, direction: str) -> dict | None:
    """Normalize one advisory proposal before any critic or Master sees it."""
    from llm_query import parse_json_output_with_mode
    import hashlib

    data, _mode = parse_json_output_with_mode(output or "")
    if not isinstance(data, dict):
        return None
    if any(data.get(key) for key in ("branch_from", "source_override", "source_v_override")):
        return None
    required = (
        "targeted_failure",
        "structural_change",
        "counterfactual",
        "measurement",
        "why_not_threshold_tuning",
    )
    normalized = {"direction": direction}
    for key in required:
        value = str(data.get(key) or "").strip()
        if len(value) < 20:
            return None
        normalized[key] = value[:3000]
    raw_files = data.get("target_files") or []
    if not isinstance(raw_files, list):
        return None
    target_files = []
    for value in raw_files[:3]:
        name = Path(str(value)).name
        if not name.endswith(".py") or name in target_files:
            continue
        target_files.append(name)
    if not target_files:
        return None
    normalized["target_files"] = target_files
    normalized["risks"] = str(data.get("risks") or "")[:2000]
    normalized["proposal_id"] = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return normalized


def _validated_proposal_critique(output: str, proposal_ids: set[str]) -> dict | None:
    from llm_query import parse_json_output_with_mode

    data, _mode = parse_json_output_with_mode(output or "")
    if not isinstance(data, dict):
        return None
    ranking = [
        str(value) for value in (data.get("ranking") or [])
        if str(value) in proposal_ids
    ]
    ranking = list(dict.fromkeys(ranking))
    if not ranking:
        return None
    rejected = [
        str(value) for value in (data.get("reject") or [])
        if str(value) in proposal_ids
    ]
    return {
        "ranking": ranking,
        "reject": list(dict.fromkeys(rejected)),
        "reason": str(data.get("reason") or "")[:2500],
    }


async def _run_master_proposal_ensemble(
    planning_context: str,
    *,
    source_v: int,
    next_v: int,
    ui,
    log_dir: Path,
    allowed_evidence_snapshot_dir: str,
) -> str:
    """Three independent proposals, two blind critics, deterministic ordering.

    The ensemble is advisory.  It cannot alter lineage, evidence cutoffs,
    executable literals, or gates; the final plan still passes the canonical
    schema/compiler/validator path.
    """
    import asyncio

    async def propose(direction: str, directive: str):
        output_contract = (
            "Return one JSON object with exactly: targeted_failure, structural_change, "
            "counterfactual, measurement, why_not_threshold_tuning, target_files "
            "(1-3 basenames), risks. Do not emit tasks, a worker plan, source choice, "
            "Markdown, or commentary."
        )
        prompt = (
            "You are an independent poker-bot mechanism proposal scout. "
            f"The system-owned source is fixed at v{source_v} and target at v{next_v}; "
            "never rerank, branch, change evidence, or change gates.\n"
            f"Distinct lens: {directive}\n"
            "Read only the allowed frozen snapshot and source/target code.\n\n"
            + planning_context
            + "\n\nFINAL SCOUT OUTPUT CONTRACT (this overrides the embedded Master output format):\n"
            + output_contract
        )
        return await run_claude_query(
            prompt,
            [],
            ui,
            f"MASTER PROPOSAL {direction}",
            log_dir / f"master_proposal_{direction}_io.txt",
            tools=["Read"],
            allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
        )

    proposal_results = await asyncio.gather(
        *(propose(direction, directive) for direction, directive in _MASTER_PROPOSAL_DIRECTIONS),
        return_exceptions=True,
    )
    proposals = []
    seen_payloads = set()
    for (direction, _directive), result in zip(_MASTER_PROPOSAL_DIRECTIONS, proposal_results):
        if isinstance(result, BaseException):
            continue
        output = result[0] if isinstance(result, tuple) else ""
        proposal = _validated_master_proposal(output, direction)
        if proposal is None:
            continue
        dedupe = json.dumps(
            {key: value for key, value in proposal.items() if key not in {"direction", "proposal_id"}},
            sort_keys=True,
            ensure_ascii=False,
        )
        if dedupe in seen_payloads:
            continue
        seen_payloads.add(dedupe)
        proposals.append(proposal)
    if not proposals:
        return (
            "Proposal ensemble unavailable or all proposals failed deterministic validation. "
            "Design the final plan directly from frozen evidence and hard contracts."
        )

    proposal_payload = json.dumps(proposals, ensure_ascii=False, indent=2)

    async def critique(name: str, lens: str):
        prompt = (
            "You are a blind advisory critic. Source, evidence cutoff, scope literals, and "
            "quality gates are immutable. Rank only the supplied proposal_id values. "
            f"Lens: {lens}\n"
            "Return JSON: {\"ranking\":[ids best-first],\"reject\":[ids with a concrete "
            "reachability/attribution flaw],\"reason\":\"...\"}.\n\n"
            + proposal_payload
            + "\n\nFINAL CRITIC OUTPUT CONTRACT: return only the ranking/reject/reason JSON; "
            "do not repeat or rewrite a proposal."
        )
        return await run_claude_query(
            prompt,
            [],
            ui,
            f"MASTER PROPOSAL CRITIC {name}",
            log_dir / f"master_proposal_critic_{name}_io.txt",
            tools=[],
        )

    critic_results = await asyncio.gather(
        critique("falsification", "Counterfactual quality, causal attribution, and evidence support."),
        critique("scope", "Reachability, bounded implementation scope, and regression risk."),
        return_exceptions=True,
    )
    proposal_ids = {item["proposal_id"] for item in proposals}
    critiques = []
    for result in critic_results:
        if isinstance(result, BaseException):
            continue
        output = result[0] if isinstance(result, tuple) else ""
        critique_row = _validated_proposal_critique(output, proposal_ids)
        if critique_row is not None:
            critiques.append(critique_row)

    # Deterministic Borda ordering.  Critic prose cannot create/delete a
    # candidate, and two independent rejections are needed to demote one.
    order = {item["proposal_id"]: index for index, item in enumerate(proposals)}
    scores = {proposal_id: 0 for proposal_id in proposal_ids}
    rejects = {proposal_id: 0 for proposal_id in proposal_ids}
    for critique_row in critiques:
        ranked = critique_row["ranking"]
        default_rank = len(proposals)
        for proposal_id in proposal_ids:
            scores[proposal_id] += (
                ranked.index(proposal_id) if proposal_id in ranked else default_rank
            )
        for proposal_id in critique_row["reject"]:
            rejects[proposal_id] += 1
    proposals.sort(
        key=lambda item: (
            rejects[item["proposal_id"]] >= 2,
            scores[item["proposal_id"]],
            order[item["proposal_id"]],
        )
    )
    packet = {
        "authority": (
            "advisory_only; final Master must obey frozen lineage/evidence and canonical "
            "runtime/schema/gate contracts"
        ),
        "proposal_count": len(proposals),
        "valid_critic_count": len(critiques),
        "ordered_proposals": proposals,
        "critic_reviews": critiques,
    }
    return _trim_to_budget(json.dumps(packet, ensure_ascii=False, indent=2), 18_000)


def _render_analysis_section(text: str, default_msg: str) -> str:
    """Map an analyst's raw return into the text injected into the Master prompt.

    - Empty/None -> default "no data" message (unchanged behaviour).
    - LLM_INFRA_SENTINEL -> explicit "LLM crashed" warning (so the Master does
      not misread a missing analysis as a negative business signal).
    - Anything else -> the actual analysis text.
    """
    if not text or not text.strip():
        return default_msg
    if text.strip() == LLM_INFRA_SENTINEL:
        return LLM_INFRA_SENTINEL_MSG
    return text


def _line_budget_summary(bot_v: int, *, baseline_label: str = "source") -> str:
    """Summarize LOC pressure for the exact baseline Workers will edit."""
    try:
        bot_dir = get_bot_dir(bot_v)
    except Exception:
        return "Line budget: unavailable."
    lines = [f"Line budget / file-size pressure ({baseline_label}={bot_dir.name}):"]
    for filename in sorted(CORE_STRATEGY_FILES):
        path = bot_dir / filename
        if not path.exists():
            continue
        try:
            count = sum(1 for _ in path.open(encoding="utf-8"))
        except Exception:
            continue
        remaining = MAX_LINES_HARD_CAP - count
        status = "ok"
        if remaining <= 100:
            status = "near_hard_cap"
        lines.append(f"- {filename}: {count}/{MAX_LINES_HARD_CAP} lines, remaining={remaining}, status={status}")
    if len(lines) == 1:
        return "Line budget: no core strategy files found."
    if any("near_hard_cap" in line for line in lines):
        lines.append(
            "MANDATORY when near_hard_cap: do LOC recovery or move cohesive logic into helper modules; "
            "do not increase that core file's line count."
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────
# Master Analysis
# ──────────────────────────────────────────────

async def _run_master_analysis(source_v, next_v, stagnation_info, ui,
                               match_analysis="", performance_verification="",
                               replay_spotlight="", bot_action_stats="",
                               battle_experience="", exploitability_weaknesses="",
                               opponent_profiles="", research_proposals="",
                               architecture_policy=None,
                               prepared_baseline=None):
    """Run Master analysis — can run concurrently with daemon evaluation."""
    master_prompt = (PROMPTS_DIR / "master_prompt.md").read_text()
    # Apply section budgets to avoid experience_pool crowding out match_analysis.
    # C-class: render the sentinel (returned when the analyst LLM crashed on an
    # infrastructure error) into an explicit warning BEFORE trimming, so the
    # Master sees "LLM crashed" rather than "no data" (which would be read as a
    # negative business signal). Non-sentinel text passes through unchanged.
    match_analysis_rendered = _render_analysis_section(
        match_analysis, "",
    )
    perf_rendered = _render_analysis_section(
        performance_verification, "No performance verification data available.",
    )
    match_analysis_trimmed = _trim_to_budget(match_analysis_rendered, 10_000, tail=True)
    perf_trimmed = _trim_to_budget(perf_rendered, 4_000)

    battle_experience_trimmed = _trim_to_budget(
        battle_experience or "No battle experience data available yet.",
        12_000,
        tail=True,
    )
    bot_action_stats_trimmed = _trim_to_budget(
        bot_action_stats or "No bot action statistics available.", 12_000)
    opponent_profiles_trimmed = _trim_to_budget(
        opponent_profiles or "No per-opponent behavior profiles available.", 8_000)
    replay_spotlight_trimmed = _trim_to_budget(
        replay_spotlight or "No replay spotlight data available.", 8_000)
    exploitability_trimmed = _trim_to_budget(
        exploitability_weaknesses or "No exploitability probe data available yet.", 6_000)
    research_trimmed = _trim_to_budget(
        research_proposals or "No web-derived research proposals this generation (run_literature_probe not triggered or returned none).", 4_000)
    # MAP-Elites fitness is updated asynchronously from live H2H.  Reopening it
    # here would bypass the generation evidence cutoff and could change a retry's
    # plan. Source/crossover diversity already uses the frozen SelectionView;
    # Master receives no second live strength ranking.
    frontier_trimmed = (
        "Frontier/MAP-Elites strength ranking omitted here; system-owned source "
        "selection already consumed the frozen diversity view."
    )
    try:
        from official_certification import official_feedback_summary
        official_feedback = _trim_to_budget(official_feedback_summary(), 6_000)
    except Exception as exc:
        official_feedback = f"Official EXE compliance feedback unavailable: {type(exc).__name__}: {str(exc)[:200]}"
    planning_baseline_v = next_v if isinstance(prepared_baseline, dict) else source_v
    planning_baseline_label = (
        "prepared_crossover_child"
        if isinstance(prepared_baseline, dict)
        else "source_parent"
    )
    try:
        from national_capability_contract import national_runtime_feedback_summary
        runtime_feedback = _trim_to_budget(
            national_runtime_feedback_summary(
                get_bot_dir(planning_baseline_v),
                source_label=(
                    f"{bot_name(planning_baseline_v)} prepared crossover baseline"
                    if isinstance(prepared_baseline, dict)
                    else bot_name(source_v)
                ),
            ),
            4_000,
        )
    except Exception as exc:
        runtime_feedback = f"National runtime architecture feedback unavailable: {type(exc).__name__}: {str(exc)[:200]}"
    if isinstance(architecture_policy, dict):
        try:
            from runtime_architecture_policy import architecture_policy_prompt
            architecture_policy_text = architecture_policy_prompt(architecture_policy)
        except Exception as exc:
            architecture_policy_text = (
                f"Runtime architecture policy rendering failed: {type(exc).__name__}: {str(exc)[:200]}"
            )
    else:
        architecture_policy_text = "System-owned runtime architecture policy: not active for this source."
    try:
        from strategy_reference_pack import master_reference_summary
        strategy_reference_packet = _trim_to_budget(master_reference_summary(), 6_000)
    except Exception as exc:
        strategy_reference_packet = (
            "Local strategy reference cards unavailable: "
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )
    try:
        from workflow_profiles import get_workflow_profile, profile_summary
        workflow_profile = get_workflow_profile()
        workflow_profile_text = profile_summary(workflow_profile)
    except Exception:
        workflow_profile = None
        workflow_profile_text = "Workflow profile: default"
    line_budget_text = _line_budget_summary(
        planning_baseline_v,
        baseline_label=planning_baseline_label,
    )
    if isinstance(prepared_baseline, dict):
        try:
            from prepared_baseline_contract import prepared_baseline_prompt

            prepared_baseline_text = _trim_to_budget(
                prepared_baseline_prompt(prepared_baseline),
                18_000,
            )
        except Exception as exc:
            prepared_baseline_text = (
                "Prepared crossover baseline rendering failed closed before this "
                f"prompt should run: {type(exc).__name__}: {str(exc)[:240]}"
            )
    else:
        prepared_baseline_text = (
            "No two-parent prepared baseline: Workers start from the copied source parent."
        )
    try:
        from evidence_snapshot import (
            h2h_snapshot_contract_text,
            load_generation_snapshot_identity,
        )
        h2h_snapshot = load_generation_snapshot_identity(next_v)
        if not h2h_snapshot.get("available"):
            raise RuntimeError(
                f"generation evidence snapshot unavailable: {h2h_snapshot.get('reason')}"
            )
        h2h_data_file = h2h_snapshot.get("h2h_relpath", "web/core/results/head_to_head.json")
        selection_data_file = h2h_snapshot.get(
            "selection_relpath",
            f"web/core/results/v{next_v}/evidence_snapshot/selection_snapshot.json",
        )
        h2h_snapshot_contract = h2h_snapshot_contract_text(next_v, source_v=source_v)
    except Exception as exc:
        ui.log_history(
            f"Master blocked: stable evaluation snapshot unavailable ({exc})",
            "error",
        )
        return None

    # eval_rounds.jsonl is a live, independently written strength view.  The
    # same information is represented by the cycle-bound selection rows, so it
    # must not be injected as a second mutable authority.
    eval_round_summary = (
        "Live eval-round summary intentionally omitted; use the frozen "
        "selection rows and rating-history tail."
    )

    master_prompt = substitute_template(master_prompt, {
        "stagnation_info": stagnation_info,
        "match_analysis": match_analysis_trimmed,
        "performance_verification": perf_trimmed,
        "source_v": str(source_v),
        "next_v": str(next_v),
        "replay_spotlight": replay_spotlight_trimmed,
        "bot_action_stats": bot_action_stats_trimmed,
        "opponent_profiles": opponent_profiles_trimmed,
        "eval_round_summary": eval_round_summary,
        "battle_experience": battle_experience_trimmed,
        "exploitability_weaknesses": exploitability_trimmed,
        "research_proposals": research_trimmed,
        "official_feedback": official_feedback,
        "runtime_feedback": runtime_feedback,
        "strategy_reference_packet": strategy_reference_packet,
        "h2h_data_file": h2h_data_file,
        "selection_data_file": selection_data_file,
        "h2h_snapshot_contract": h2h_snapshot_contract,
        "master_plan_executable_contract": master_plan_executable_contract_text(),
    })
    master_ctx = (
        f"Current evolution: v{source_v} → v{next_v}\n"
        f"Source bot directory (read-only parent): {bot_relpath(source_v)}/\n"
        f"Target bot directory (workers edit/verify): {bot_relpath(next_v)}/\n"
        f"Planning baseline: {bot_relpath(planning_baseline_v)}/ ({planning_baseline_label})\n"
        f"Selection evidence snapshot: {selection_data_file}\n"
        f"Use only that digest-bound snapshot for ratings, RD, games, coverage, trends, and ranking; "
        f"do not reopen live glicko_ratings.json, bot_stats.json, or rating_history.jsonl.\n"
        f"Head-to-Head data snapshot: {h2h_data_file}\n"
        f"Do not read live H2H for matchup counts during planning; use the snapshot above.\n"
        f"Experience/lesson evidence is frozen as bounded injected prompt excerpts; do not reopen live results files.\n"
        f"\n{h2h_snapshot_contract}\n"
        f"\n{workflow_profile_text}\n"
        f"\n{frontier_trimmed}\n"
        f"\nOfficial EXE Compliance Feedback:\n{official_feedback}\n"
        f"\nNational Runtime Architecture Feedback:\n{runtime_feedback}\n"
        f"\nPrepared Baseline Contract:\n{prepared_baseline_text}\n"
        f"\n{architecture_policy_text}\n"
        f"\n{line_budget_text}\n"
    )
    master_log_file = get_logs_dir(next_v) / "master_io.txt"

    try:
        proposal_ensemble = await _run_master_proposal_ensemble(
            master_prompt + "\n" + master_ctx,
            source_v=int(source_v),
            next_v=int(next_v),
            ui=ui,
            log_dir=master_log_file.parent,
            allowed_evidence_snapshot_dir=str(
                Path(h2h_snapshot["manifest_path"]).parent
            ),
        )
    except Exception as exc:
        proposal_ensemble = (
            "Proposal ensemble infrastructure unavailable; it has no authority over "
            f"the final plan. error={type(exc).__name__}:{str(exc)[:240]}"
        )
    master_ctx += (
        "\n# Weak-model proposal ensemble (advisory, prefiltered)\n"
        + proposal_ensemble
        + "\nSelect or synthesize one attributable mechanism. Do not copy multiple "
        "unrelated proposals into one generation, and do not treat critic votes as "
        "permission to change source, evidence, scope, or gates.\n"
    )

    for attempt in range(MAX_MASTER_RETRIES):
        ui.clear_io()
        try:
            output, _, _ = await run_claude_query(
                master_prompt + "\n" + master_ctx, [], ui,
                f"MASTER (Try {attempt+1})", master_log_file,
                tools=["Read"],
                allowed_evidence_snapshot_dir=str(
                    Path(h2h_snapshot["manifest_path"]).parent
                ),
            )
        except Exception as exc:
            _final_mode = f"LLM_EXCEPTION:{type(exc).__name__}"
            try:
                ui.log_history(
                    f"Master LLM call failed ({type(exc).__name__}): {str(exc)[:240]}",
                    "error",
                )
            except Exception:
                pass
            try:
                from system_log import log_system_event
                log_system_event(
                    "pipeline.master_llm_call_failed",
                    "error",
                    (
                        f"Master v{next_v} try {attempt+1} LLM call failed: "
                        f"{type(exc).__name__}: {str(exc)[:240]}"
                    ),
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "attempt": attempt + 1,
                        "failure_mode": _final_mode,
                        "exception_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                )
            except Exception:
                pass
            import hashlib

            raise MasterInfrastructureError(
                source_v,
                next_v,
                hashlib.sha256(
                    (master_prompt + "\n" + master_ctx).encode("utf-8")
                ).hexdigest(),
                f"{type(exc).__name__}: {str(exc)[:400]}",
            ) from exc
        # A2 (v125 retry-storm fix): classify the parse failure so the log
        # distinguishes NO_FENCE (model never emitted JSON) / NO_JSON (empty) /
        # PARSE_ERROR (had JSON but unparseable) — instead of the undifferentiated
        # "malformed JSON" that hid three distinct root causes.
        from llm_query import parse_json_output_with_mode
        data, _failure_mode = parse_json_output_with_mode(output)
        if data and "tasks" in data:
            # The structured runtime contract and reference-card choice already
            # determine a small set of literal execution anchors.  Bind those
            # system-owned terms before Pydantic validation instead of asking a
            # weaker planner model to reproduce them losslessly in free prose.
            # Invalid contracts are intentionally left untouched and still fail
            # the canonical schema gate below.
            from plan_compiler import bind_system_owned_worker_contract_terms
            data, _binding_meta = bind_system_owned_worker_contract_terms(data)
            if _binding_meta.get("bound"):
                ui.log_history(
                    "Master plan contract compiler bound missing execution anchors "
                    f"for {len(_binding_meta.get('bound_tasks', []))} worker task(s).",
                    "info",
                )
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.master_contract_terms_bound",
                        "info",
                        f"Master v{next_v}: bound system-owned worker contract terms",
                        {
                            "next_v": next_v,
                            "source_v": source_v,
                            "attempt": attempt + 1,
                            "binding": _binding_meta,
                        },
                    )
                except Exception:
                    pass
            # P0 修复：在 Pydantic 剥离 branch_from (extra='ignore') 之前，对原始 dict
            # 跑 Master 的 source-override 硬校验。MasterPlan 删除 branch_from 字段后，
            # model_validate 会静默丢弃该键，必须在丢弃前拦截。
            from tool_planning import _validate_master_plan
            # Backward-compat: this pre-schema check only needs to catch source
            # override fields before Pydantic strips unknown keys. The canonical
            # Master validation, including EXHAUSTED-direction hard gating, runs
            # in tool_planning.run_master after plan normalization/audit context.
            _errs, _ = _validate_master_plan(data, exhausted_policy="warn")
            _src_override = any(data.get(f) for f in ("branch_from", "source_override", "source_v_override"))
            if _src_override:
                ui.log_history(
                    f"Master plan rejected: must not set branch_from. "
                    f"({_errs})",
                    "warn",
                )
                import asyncio as _asyncio
                await _asyncio.sleep(2)
                continue
            from output_schema import validate_agent_output
            data, errors = validate_agent_output("master", data)
            if errors:
                ui.log_history(f"Master plan validation issues: {'; '.join(errors[:3])}", "warn")
                # Hard gate: inject schema errors into the next retry's prompt so
                # the Master re-emits strictly schema-conformant JSON rather than
                # silently returning the malformed plan. errors text is truncated
                # to avoid unbounded prompt growth across retries.
                if attempt + 1 < MAX_MASTER_RETRIES:
                    err_block = "\n".join(f"- {e}" for e in errors)[:1500]
                    master_prompt = (
                        master_prompt
                        + "\n\n# 上一轮计划校验失败，必须修正：\n"
                        + err_block
                        + "\n请重新输出严格符合 schema 的 JSON。"
                    )
                    ui.log_history("Master plan rejected by schema. Retrying with errors...", "warn")
                    import asyncio
                    await asyncio.sleep(2)
                    continue
                # Retries exhausted: fail closed. A malformed plan cannot become
                # an executable worker contract merely because retries ran out.
                ui.log_history(
                    f"Master plan still violates schema after {MAX_MASTER_RETRIES} retries; "
                    "rejecting generation plan.",
                    "error"
                )
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.master_schema_gate_exhausted", "error",
                        f"Master plan schema validation failed after {MAX_MASTER_RETRIES} retries: "
                        + "; ".join(errors[:5]),
                    )
                except Exception:
                    pass
                return None
            # SUCCESS path (BUGFIX, root cause of the v107–v127 Master deadlock):
            # the plan parsed with `tasks`, carries no branch_from override, and
            # passed schema validation with NO errors. This `return data` was
            # MISSING for 11+ generations: every valid plan fell through to the
            # "Master output malformed JSON" branch below, burned all
            # MAX_MASTER_RETRIES, and returned None. The SDK-signature fix
            # (48b51f2/c537ff1) only cured the EMPTY-output case — once plans
            # came back non-empty and valid, this missing return STILL discarded
            # them, which is exactly why "malformed-JSON persists post-fix" was
            # observed. NOT a schema/SDK-sig/direction-audit problem.
            ui.log_history("Master plan accepted (valid JSON, schema-clean).", "info")
            # RC1 (success-path symmetry): emit the success terminal event here so
            # the clean-success path is as visible as the failure paths above. The
            # degraded path (:177) already emits pipeline.master_schema_gate_exhausted
            # (error) — only this clean branch was event-silent. Without it, a
            # master-success-return-bug regression (valid plan parsed but the
            # function then failed to return) is invisible in the event stream;
            # prepare_done=N vs master_plan_accepted=0 would now expose it at once.
            try:
                from event_bus import success
                success("pipeline.master_plan_accepted",
                        f"Master v{next_v} plan accepted (schema-clean, try {attempt+1})",
                        next_v=next_v, source_v=source_v,
                        master_try=attempt + 1,
                        num_tasks=len(data.get("tasks", [])))
            except Exception:
                pass
            return data
        ui.log_history(
            f"Master output malformed JSON (mode={_failure_mode}). Retrying...",
            "warn",
        )
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.master_malformed_json", "warn",
                f"Master v{next_v} try {attempt+1} output parse failed (mode={_failure_mode})",
                {"next_v": next_v, "source_v": source_v, "attempt": attempt + 1,
                 "failure_mode": _failure_mode, "output_len": len(output or "")},
            )
        except Exception:
            pass
        import asyncio
        await asyncio.sleep(2)

    _final_mode = locals().get("_failure_mode", "UNKNOWN")
    ui.log_history(
        f"Master failed to plan after {MAX_MASTER_RETRIES} retries (last mode={_final_mode}).",
        "error",
    )
    try:
        from system_log import log_system_event
        log_system_event(
            "pipeline.master_failed_to_plan", "error",
            f"Master v{next_v} failed to plan after {MAX_MASTER_RETRIES} retries (last mode={_final_mode})",
            {"next_v": next_v, "source_v": source_v,
             "last_failure_mode": _final_mode, "retries": MAX_MASTER_RETRIES},
        )
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────
# Match Analysis
# ──────────────────────────────────────────────

async def _analyze_recent_matches(source_v, ui, max_matches=8, *, next_v=None):
    """Use LLM to analyze recent replay data for the current bot.

    Collects both recent losses and close wins (margin < 3 games) to give
    the Master a balanced view of weaknesses and what's working.

    Returns a match analysis string to inject into Master's context, or ""
    if no replay data is available.
    """
    source_bot_name = bot_name(source_v)

    recent_losses = []
    close_wins = []
    from rating_snapshot import _admitted_70_hand_history_sample
    if next_v is not None:
        from evidence_snapshot import load_generation_evaluation_snapshot

        frozen = load_generation_evaluation_snapshot(next_v)
        if not frozen.get("available"):
            return ""
        history_index = frozen.get("match_history_index") or {}
        entries = history_index.get("entries") or []
    else:
        if not MATCH_HISTORY_FILE.exists():
            return ""
        entries = []
        with locked_file(MATCH_HISTORY_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entries.append(entry)

    for entry in entries:
            if not isinstance(entry, dict):
                continue
            if _admitted_70_hand_history_sample(entry) is None:
                continue

            b0, b1 = entry.get("bot0"), entry.get("bot1")
            w0, w1 = entry.get("bot0_wins", 0), entry.get("bot1_wins", 0)

            if b0 == source_bot_name:
                bot_wins, opp_wins = w0, w1
            elif b1 == source_bot_name:
                bot_wins, opp_wins = w1, w0
            else:
                continue

            if opp_wins > bot_wins:
                recent_losses.append(entry)
            elif bot_wins > opp_wins and (bot_wins - opp_wins) <= 2:
                # Close win (margin ≤ 2 games) — reveals near-miss vulnerabilities
                close_wins.append(entry)

    if not recent_losses and not close_wins:
        return ""

    recent_losses = recent_losses[-max_matches:]
    close_wins = close_wins[-(max_matches // 2):]

    def _load_summaries(entries, label):
        result = []
        for entry in entries:
            replay_path = REPLAY_DIR / entry["id"]
            if not replay_path.exists():
                continue
            try:
                with locked_file(replay_path, "r") as rf:
                    replay_data = json.load(rf)
                if replay_data.get("evaluation_identity_digest") != entry.get(
                    "evaluation_identity_digest"
                ):
                    continue
                summary = summarize_replay_for_analysis(
                    replay_data,
                    source_bot_name,
                )
                if summary:
                    result.append(f"[{label}] {summary}")
            except (json.JSONDecodeError, OSError):
                continue
        return result

    summaries = _load_summaries(recent_losses, "LOSS") + _load_summaries(close_wins, "CLOSE WIN")

    if not summaries:
        return ""

    # Load template and substitute
    template_file = PROMPTS_DIR / "match_analyst.md"
    if not template_file.exists():
        return ""
    match_analyst_prompt = template_file.read_text()
    match_analyst_prompt = substitute_template(match_analyst_prompt, {
        "match_summaries": "\n\n".join(summaries),
    })

    log_file = get_logs_dir(source_v) / "match_analyst_io.txt"
    try:
        output, _, _ = await run_claude_query(
            match_analyst_prompt, [], ui,
            "MATCH ANALYST", log_file,
        )
        if not output or not output.strip():
            # Retry once if match analyst returned empty (529/timeout)
            output, _, _ = await run_claude_query(
                match_analyst_prompt, [], ui,
                "MATCH ANALYST (retry)", log_file,
            )
        return output or ""
    except Exception as e:
        # C-class: distinguish LLM infrastructure crash from "no data".
        # Return a sentinel string so the Master prompt builder can surface
        # "analysis unavailable due to LLM failure" instead of the misleading
        # "No match analysis data available". Return type stays str for compat.
        from llm_failure import is_llm_infra_error
        if is_llm_infra_error(e):
            ui.log_history(f"Match analysis LLM infrastructure error: {e}", "warn")
            from system_log import log_system_event
            log_system_event("pipeline.match_analyst_infra", "warn",
                             f"Match analyst v{source_v} LLM crashed (infra): {e}",
                             {"source_v": source_v, "error": str(e)})
            return "[LLM_INFRA_ERROR: analysis unavailable]"
        ui.log_history(f"Match analysis failed: {e}", "warn")
        return ""
