"""Review-stage LLM agents: Critic and Crossover.

These agents evaluate worker output and verify strategic improvements.
"""

import difflib
import hashlib
import json
import os
import stat
from pathlib import Path

from logging_config import get_logger
_log = get_logger("review")

from llm_failure import infra_payload
from llm_availability import LLMAvailabilityBlocked

from evolution_infra import (
    run_claude_query, substitute_template,
    get_bot_dir, get_logs_dir,
    verify_code, run_import_contract_test,
    PROMPTS_DIR, RESULTS_DIR,
    MAX_CROSSOVER_RETRIES, copy_bot_tree_for_candidate,
)


def _render_critic_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "source_v", "next_v", "master_plan", "code_evidence",
        "h2h_snapshot_contract", "previous_critic", "invocation_id",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Strategy Critic renderer input contract mismatch")
    code_evidence = inputs["code_evidence"]
    previous = inputs["previous_critic"]
    if not isinstance(code_evidence, dict) or (
        previous is not None and not isinstance(previous, dict)
    ):
        raise ValueError("Strategy Critic typed evidence mismatch")
    source_v = int(inputs["source_v"])
    next_v = int(inputs["next_v"])
    master_plan = str(inputs["master_plan"])
    template = (
        Path(__file__).resolve().parent / "prompts" / "critic_prompt.md"
    ).read_text(encoding="utf-8")
    text = substitute_template(template, {
        "master_plan": master_plan,
        "version": str(next_v),
        "parent_version": str(source_v),
        "critic_lineage_contract": str(code_evidence["lineage_contract"]),
        "critic_evaluation_steps": str(code_evidence["evaluation_steps"]),
    })
    text += "\n\n" + str(code_evidence["prompt_section"])
    text += "\n\n" + str(inputs["h2h_snapshot_contract"])
    if previous:
        prev_score = previous.get("score", 0)
        prev_feedback = str(previous.get("feedback") or "")[:1000]
        text += (
            "\n\n# Previous Critic Evaluation (for context — you are evaluating an UPDATED version):\n"
            f"- Previous Score: {prev_score}\n"
            f"- Previous Approved: {previous.get('approved', False)}\n"
            "- Previous Feedback (each point MUST be explicitly addressed):\n"
            f"{prev_feedback}\n"
            "\nYou MUST verify that EACH specific point from the previous feedback was addressed.\n"
            "If ANY previous issue remains unresolved, do NOT raise the score above the previous score.\n"
            "If improvements were made that address ALL feedback points, raise the score accordingly.\n"
        )
    from strategy_reference_pack import current_strict_runtime_prompt_overlay

    text += (
        "\n\n" + current_strict_runtime_prompt_overlay()
        + "\n\nYou remain advisory: report a violation for deterministic gates "
        "to decide; never certify, accept, reject, or schedule a repair yourself."
    )
    invocation_id = str(inputs["invocation_id"] or "")
    if invocation_id:
        text += (
            "\n\nSYSTEM CALL BINDING (copying this value does not grant authority): "
            f"invocation_id={invocation_id}; "
            "purpose=system_strict_bootstrap_gate:critic."
        )

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="critic_plan_candidate_snapshot",
        evidence_provenance={
            "source_v": source_v,
            "next_v": next_v,
            "master_plan_digest": hashlib.sha256(
                master_plan.encode("utf-8")
            ).hexdigest(),
            "code_evidence_digest": hashlib.sha256(
                json.dumps(
                    code_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "h2h_snapshot_digest": hashlib.sha256(
                str(inputs["h2h_snapshot_contract"]).encode("utf-8")
            ).hexdigest(),
            "previous_critic_digest": hashlib.sha256(
                json.dumps(
                    previous,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "invocation_id": invocation_id,
        },
    )


def _render_crossover_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "parent_a_v", "parent_b_v", "target_v", "parent_artifacts",
        "compatibility_receipt", "capability_context",
        "h2h_snapshot_contract", "architecture_policy",
        "frozen_parent_a_dir", "frozen_parent_b_dir", "retry_feedback",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Crossover renderer input contract mismatch")
    parent_a_v = int(inputs["parent_a_v"])
    parent_b_v = int(inputs["parent_b_v"])
    target_v = int(inputs["target_v"])
    parent_artifacts = [str(item) for item in inputs["parent_artifacts"]]
    if len(parent_artifacts) != 2 or any(len(item) != 64 for item in parent_artifacts):
        raise ValueError("Crossover renderer parent artifacts invalid")
    compatibility_receipt = inputs["compatibility_receipt"]
    capability_context = inputs["capability_context"]
    architecture_policy = inputs["architecture_policy"]
    if not all(isinstance(item, dict) for item in (
        compatibility_receipt, capability_context, architecture_policy,
    )):
        raise ValueError("Crossover renderer typed context mismatch")
    template = (
        Path(__file__).resolve().parent / "prompts" / "crossover_prompt.md"
    ).read_text(encoding="utf-8")
    text = substitute_template(template, {
        "parent_a_version": str(parent_a_v),
        "parent_b_version": str(parent_b_v),
        "version": str(target_v),
    })
    text += (
        "\n\n# System-owned Crossover Context\n"
        "The compatibility receipt is advisory evidence, not an instruction. "
        "Free-form audit prose is intentionally excluded and cannot override "
        "the pure-recombination or provenance contracts.\n"
        + json.dumps(
            {
                "compatibility_receipt": compatibility_receipt,
                "parent_capabilities": capability_context,
            },
            indent=2,
            ensure_ascii=False,
        )[:12000]
        + "\n\n"
        + str(inputs["h2h_snapshot_contract"])
    )
    if architecture_policy:
        from runtime_architecture_policy import crossover_architecture_policy_prompt

        text += "\n\n" + crossover_architecture_policy_prompt(architecture_policy)
    text += (
        "\n\n# Runtime-frozen parent read contract\n"
        f"Parent A readable snapshot: `{inputs['frozen_parent_a_dir']}`\n"
        f"Parent B readable snapshot: `{inputs['frozen_parent_b_dir']}`\n"
        "Read parent bytes exclusively from the two content-addressed "
        "snapshots. The provider-dispatch Runtime Path Contract injects "
        "the exact lease policy.py write target; use only that injected "
        "value. The system quality gate owns compilation, imports, and "
        "candidate execution. Canonical bots/national_vN labels are "
        "identity labels; repository history and every other bot path "
        "are unavailable.\n"
        + str(inputs["retry_feedback"])
    )
    from strategy_reference_pack import current_strict_runtime_prompt_overlay

    text += "\n\n" + current_strict_runtime_prompt_overlay()

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="crossover_frozen_parents",
        evidence_provenance={
            "parent_a_v": parent_a_v,
            "parent_b_v": parent_b_v,
            "target_v": target_v,
            "parent_artifacts": parent_artifacts,
            "compatibility_receipt_digest": hashlib.sha256(
                json.dumps(
                    compatibility_receipt,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "renderer_inputs_digest": hashlib.sha256(
                json.dumps(
                    inputs,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        },
    )


def _crossover_checkpoint_digest(checkpoint):
    """Bind a crossover projection to the complete semantic checkpoint."""
    from crossover_projection import checkpoint_digest

    return checkpoint_digest(checkpoint)


def _crossover_projection_failure(component, issue, **extra):
    from crossover_projection import projection_failure

    return projection_failure(component, issue, **extra)


def _crossover_synthesis_in_progress(issue):
    """A concurrent valid lease is a retry signal, not infrastructure drift."""
    return {
        "success": False,
        "outcome": "concurrent_effect_in_progress",
        "failure_class": "concurrency",
        "component": "crossover_synthesis_effect",
        "issue": str(issue),
    }


def _crossover_target_identity(target_dir):
    """Return the canonical target preimage without treating absence as data."""
    from crossover_projection import target_identity

    return target_identity(target_dir)


def _project_crossover_candidate(
    *,
    workspace,
    target_dir,
    parent_a_v,
    parent_b_v,
    target_v,
    attempt,
    compatibility,
    architecture_policy,
    synthesis_receipt,
    entry_checkpoint,
    entry_target_identity,
    preimage_artifact_hash,
    workflow_store,
    artifact_store,
):
    """Atomically publish one validated isolated crossover attempt.

    The LLM and every candidate gate run against ``workspace``.  Only this
    actor-serialized projection may replace the canonical child or advance the
    checkpoint, and both are guarded by immutable preimages plus checkpoint
    compare-and-swap.
    """
    from crossover_projection import project_crossover_candidate

    return project_crossover_candidate(
        workspace=workspace,
        target_dir=target_dir,
        parent_a_v=parent_a_v,
        parent_b_v=parent_b_v,
        target_v=target_v,
        attempt=attempt,
        compatibility=compatibility,
        architecture_policy=architecture_policy,
        synthesis_receipt=synthesis_receipt,
        entry_checkpoint=entry_checkpoint,
        entry_target_identity=entry_target_identity,
        preimage_artifact_hash=preimage_artifact_hash,
        workflow_store=workflow_store,
        artifact_store=artifact_store,
    )


_CRITIC_CODE_EVIDENCE_MAX_CHARS = 400_000


def _read_regular_policy(path: Path) -> tuple[str, str]:
    """Read one already-gated policy without following a mutable symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"critic_policy_not_regular:{path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _CRITIC_CODE_EVIDENCE_MAX_CHARS:
                raise RuntimeError(
                    f"critic_policy_exceeds_prompt_evidence_limit:{path}:{total}"
                )
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > _CRITIC_CODE_EVIDENCE_MAX_CHARS:
        raise RuntimeError(
            f"critic_policy_exceeds_prompt_evidence_limit:{path}:{len(raw)}"
        )
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def _critic_code_evidence(
    next_v: int,
    source_v: int,
    *,
    protocol_bootstrap_prepared_only: bool,
    target_dir: str | Path | None = None,
) -> dict[str, str]:
    """Build the exact, system-owned code envelope consumed by the Critic."""

    from bot_artifact import hash_path
    from bot_namespace import bot_name, bot_relpath

    target_dir = (
        Path(target_dir)
        if target_dir is not None
        else get_bot_dir(int(next_v))
    )
    target_policy, target_policy_hash = _read_regular_policy(
        target_dir / "policy.py"
    )
    target_artifact_hash = hash_path(target_dir)
    if protocol_bootstrap_prepared_only:
        body = (
            "# SYSTEM-SUPPLIED STRICT BOOTSTRAP POLICY\n"
            f"target={bot_relpath(next_v)}/policy.py\n"
            f"target_artifact_sha256={target_artifact_hash}\n"
            f"target_policy_sha256={target_policy_hash}\n"
            "Historical completion high-water is not a source or comparison.\n"
            "```python\n"
            + target_policy
            + ("\n" if not target_policy.endswith("\n") else "")
            + "```"
        )
        return {
            "lineage_contract": (
                f"Prepared `{bot_relpath(next_v)}/` is the sole readable code "
                f"baseline. v{source_v} is numeric high-water only, not a parent, "
                "tag, opponent, or readable path."
            ),
            "evaluation_steps": (
                "Evaluate the system-supplied prepared policy against the frozen "
                "Master plan and national ABI. Read only changed functions inside "
                f"`{bot_relpath(next_v)}/policy.py` when more context is required. "
                "Do not open or compare any historical bot."
            ),
            "prompt_section": body,
            # Structured identities let strict authority prove that the exact
            # envelope rendered for the provider belongs to the same artifact
            # it bound before dispatch.  The prompt text remains the human-
            # readable copy of these values.
            "target_artifact_hash": target_artifact_hash,
            "target_policy_hash": target_policy_hash,
        }

    from national_runtime_authority import strict_published_bot_names

    source_label = bot_name(int(source_v))
    if source_label not in set(strict_published_bot_names()):
        raise RuntimeError("critic_source_not_current_epoch_strict_published")
    source_dir = get_bot_dir(int(source_v))
    source_policy, source_policy_hash = _read_regular_policy(
        source_dir / "policy.py"
    )
    source_artifact_hash = hash_path(source_dir)
    diff = "\n".join(difflib.unified_diff(
        source_policy.splitlines(),
        target_policy.splitlines(),
        fromfile=f"{bot_relpath(source_v)}/policy.py",
        tofile=f"{bot_relpath(next_v)}/policy.py",
        lineterm="",
    ))
    if len(diff) > _CRITIC_CODE_EVIDENCE_MAX_CHARS:
        raise RuntimeError(
            f"critic_exact_diff_exceeds_prompt_evidence_limit:{len(diff)}"
        )
    diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    body = (
        "# SYSTEM-SUPPLIED EXACT PARENT-TO-TARGET POLICY DIFF\n"
        f"source={bot_relpath(source_v)}/policy.py\n"
        f"source_artifact_sha256={source_artifact_hash}\n"
        f"source_policy_sha256={source_policy_hash}\n"
        f"target={bot_relpath(next_v)}/policy.py\n"
        f"target_artifact_sha256={target_artifact_hash}\n"
        f"target_policy_sha256={target_policy_hash}\n"
        f"unified_diff_sha256={diff_hash}\n"
        "```diff\n"
        + (diff or "(no policy.py byte difference)")
        + "\n```"
    )
    return {
        "lineage_contract": (
            f"The exact current-epoch strict source is `{bot_relpath(source_v)}/`; "
            f"the candidate is `{bot_relpath(next_v)}/`. The system-supplied "
            "content-bound policy diff below is the complete code comparison."
        ),
        "evaluation_steps": (
            "Use the system-supplied exact policy diff. Read only the cited target "
            "functions when more context is required. Do not run Bash, Git history, "
            "or construct another lineage comparison."
        ),
        "prompt_section": body,
        "source_artifact_hash": source_artifact_hash,
        "source_policy_hash": source_policy_hash,
        "target_artifact_hash": target_artifact_hash,
        "target_policy_hash": target_policy_hash,
        "unified_diff_hash": diff_hash,
    }


def _critic_h2h_snapshot_material(
    next_v: int,
    source_v: int,
) -> tuple[str, Path | None]:
    """Return the exact bounded H2H prompt contract and its read scope."""

    allowed_evidence_snapshot_dir = None
    try:
        from evidence_snapshot import h2h_snapshot_contract_text

        contract = h2h_snapshot_contract_text(
            next_v,
            source_v=source_v,
            include_json=True,
            # This is the pre-existing prompt boundary.  Strict authority may
            # retain the resulting text, but it never reopens a larger live
            # snapshot or an unbounded replay/history source.
            max_chars=12000,
        )
        from evidence_snapshot import load_generation_snapshot_identity

        snapshot_identity = load_generation_snapshot_identity(next_v)
        if snapshot_identity.get("available"):
            allowed_evidence_snapshot_dir = Path(
                snapshot_identity["manifest_path"]
            ).parent
    except Exception as exc:
        contract = (
            "# Stable H2H Snapshot Contract\n"
            "The generation snapshot is unavailable. Treat all matchup strength "
            f"claims as unknown; do not read live H2H files. ({type(exc).__name__})\n"
        )
    return contract, allowed_evidence_snapshot_dir


async def _run_critic(
    next_v,
    source_v,
    master_plan_str,
    ui,
    prev_critic_result=None,
    execution_invocation_id=None,
    strict_authority=None,
):
    """Poker Strategy Critic — independently scores the strategic value of worker changes.

    Separate from the Reviewer (which checks code correctness and role boundaries).
    The Critic evaluates whether the diff will actually improve poker win rate.

    Returns a dict: {score, approved, strategic_assessment, feedback, local_optima_warning}.
    Returns ``llm_failed`` on role/tooling failure so the caller can retry the
    same gate without fabricating a strategic rejection.
    """
    critic_prompt_path = (
        Path(__file__).resolve().parent / "prompts" / "critic_prompt.md"
    )
    if not critic_prompt_path.exists():
        ui.log_history("Critic prompt not found; critic verdict is unavailable.", "error")
        return {
            "llm_failed": True,
            "error": "critic_prompt_missing",
            "approved": None,
        }

    code_evidence = None
    h2h_snapshot_contract = None
    if strict_authority is not None:
        # Provider scope is part of the same descriptor as the exact H2H text;
        # never reopen live snapshot identity after strict call creation.
        from strict_authority_workflow import (
            gate_provider_evidence_snapshot_dir,
        )

        allowed_evidence_snapshot_dir = (
            gate_provider_evidence_snapshot_dir(strict_authority)
        )
    else:
        h2h_snapshot_contract, allowed_evidence_snapshot_dir = (
            _critic_h2h_snapshot_material(int(next_v), int(source_v))
        )
        try:
            code_evidence = _critic_code_evidence(
                int(next_v),
                int(source_v),
                protocol_bootstrap_prepared_only=False,
            )
        except Exception as exc:
            ui.log_history(
                "Critic exact code envelope unavailable; no advisory verdict was created: "
                f"{type(exc).__name__}: {str(exc)[:180]}",
                "error",
            )
            return {
                "llm_failed": True,
                "error": (
                    "critic_exact_code_evidence_unavailable:"
                    f"{type(exc).__name__}:{str(exc)[:300]}"
                ),
                "approved": None,
            }

    log_file = get_logs_dir(next_v) / "critic_io.txt"
    if strict_authority is not None:
        from strict_authority_workflow import strict_invocation_log_path

        log_file = strict_invocation_log_path(
            strict_authority,
            logs_dir=log_file.parent,
            basename=log_file.name,
        )
        execution_invocation_id = strict_authority.get("invocation_id")
    try:
        if strict_authority is not None:
            # The strict descriptor owns every semantic renderer input.  Do
            # not reopen candidate/H2H/checkpoint data between descriptor
            # creation and provider rendering.
            from strict_authority_workflow import render_gate_provider_prompt

            rendered_prompt = render_gate_provider_prompt(strict_authority)
        else:
            from llm_query import render_llm_prompt

            rendered_prompt = render_llm_prompt(
                "STRATEGY CRITIC",
                producer=_render_critic_provider_prompt,
                renderer_inputs={
                    "source_v": int(source_v),
                    "next_v": int(next_v),
                    "master_plan": str(master_plan_str),
                    "code_evidence": code_evidence,
                    "h2h_snapshot_contract": h2h_snapshot_contract,
                    "previous_critic": prev_critic_result,
                    "invocation_id": str(execution_invocation_id or ""),
                },
            )
        output, cost_usd, usage = await run_claude_query(
            rendered_prompt, [], ui, "STRATEGY CRITIC", log_file,
            tools=["Read"],
            allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
            allowed_read_dirs=(
                [get_bot_dir(int(next_v))]
                if strict_authority is not None
                else [
                    get_bot_dir(int(source_v)),
                    get_bot_dir(int(next_v)),
                ]
            ),
            strict_authority=strict_authority,
        )
        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data and "score" in data:
            # Coerce non-string feedback to string (LLM sometimes returns null/list/dict)
            if "feedback" in data and not isinstance(data["feedback"], str):
                data["feedback"] = str(data["feedback"]) if data["feedback"] is not None else ""
            # Normalise: score >= 6 → approved
            from output_schema import validate_agent_output
            data, errors = validate_agent_output("critic", data)
            if errors:
                ui.log_history(f"Critic validation issues: {'; '.join(errors[:3])}", "warn")
            if "approved" not in data:
                data["approved"] = data["score"] >= 6
            data.setdefault("local_optima_warning", False)
            if execution_invocation_id is not None:
                from system_strict_bootstrap import llm_result_digest

                data["_llm_execution_material"] = {
                    "invocation_id": str(execution_invocation_id),
                    "purpose": "system_strict_bootstrap_gate:critic",
                    "role": "STRATEGY CRITIC",
                    "prompt_digest": str(
                        (strict_authority or {}).get("prompt_digest") or ""
                    ),
                    "raw_output_digest": hashlib.sha256(
                        (output or "").encode("utf-8")
                    ).hexdigest(),
                    "result_digest": llm_result_digest(cost_usd, usage),
                    "log_file": str(log_file),
                }
            return data
    except LLMAvailabilityBlocked:
        raise
    except Exception as e:
        from strict_authority_workflow import StrictAuthorityError

        if isinstance(e, StrictAuthorityError):
            raise
        ui.log_history(f"Critic execution error (NOT a strategic rejection): {e}", "warn")
        return infra_payload(e, approved=None)

    # Parse collapse: reaching here means the LLM output failed to parse
    # (NO_JSON/NO_FENCE/PARSE_ERROR) or lacked the score key, OR an exception
    # skipped the parse entirely. Previously this was an opaque "not valid JSON"
    # default. Emit a classifiable failure event so the parse collapse is visible.
    _fm = locals().get("failure_mode", "EXCEPTION")
    _out = locals().get("output", "") or ""
    try:
        from event_bus import warn
        warn("pipeline.critic_parse_failed",
             f"Critic v{next_v} parse failed (mode={_fm}); defaulting to rejected",
             version=next_v, source_v=source_v, failure_mode=_fm, output_len=len(_out))
    except Exception:
        pass
    return {
        "llm_failed": True,
        "approved": None,
        "error": f"critic_output_unusable:{_fm}",
        "feedback": "Critic output was not valid JSON.",
        "parse_failed": True,
    }


async def _run_crossover(
    parent_a_v,
    parent_b_v,
    target_v,
    ui,
    *,
    compatibility=None,
    architecture_policy=None,
    capability_context=None,
):
    """Run crossover between two elite bots to create a new child bot."""
    import shutil
    import tempfile

    from evolution_infra import read_pipeline_checkpoint
    from worker_workflow import WorkerArtifactStore, workflow_run_id
    from workflow_kernel import WorkflowStore

    crossover_prompt_path = (
        Path(__file__).resolve().parent / "prompts" / "crossover_prompt.md"
    )
    if not crossover_prompt_path.exists():
        ui.log_history("Crossover prompt not found — skipping crossover.", "error")
        return False
    compatibility = compatibility if isinstance(compatibility, dict) else {}
    parent_snapshot_receipt = compatibility.get("parent_snapshot_receipt")
    compatibility_score = compatibility.get("compatibility_score")
    if not isinstance(compatibility_score, (int, float, str, type(None))):
        compatibility_score = str(compatibility_score)[:100]

    def _bounded_guidance_files(field):
        return sorted({
            str(item).strip()[:200]
            for item in compatibility.get(field) or []
            if str(item).strip()
        })[:20]

    compatibility_receipt = {
        "compatible": bool(compatibility.get("compatible", True)),
        "compatibility_score": compatibility_score,
        "conflict_area_count": len(compatibility.get("conflict_areas") or []),
        "files_to_take_from_a": _bounded_guidance_files("files_to_take_from_a"),
        "files_to_take_from_b": _bounded_guidance_files("files_to_take_from_b"),
        "advisory_only": True,
        "parent_snapshot_receipt": parent_snapshot_receipt,
    }
    # This frozen receipt, rather than the audit's unbounded prose, is the
    # complete compatibility guidance shown to the synthesis model.  The same
    # value is persisted in the LLM effect and final crossover receipt.
    compatibility_receipt = json.loads(json.dumps(
        compatibility_receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ))
    frozen_capability_context = json.loads(json.dumps(
        capability_context or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ))
    allowed_evidence_snapshot_dir = None
    try:
        from evidence_snapshot import (
            h2h_snapshot_contract_text,
            load_generation_snapshot_identity,
        )

        h2h_snapshot_contract = h2h_snapshot_contract_text(
            target_v,
            source_v=parent_a_v,
            include_json=True,
            max_chars=24_000,
        )
        snapshot_identity = load_generation_snapshot_identity(target_v)
        if snapshot_identity.get("available"):
            allowed_evidence_snapshot_dir = Path(
                snapshot_identity["manifest_path"]
            ).parent
    except Exception as exc:
        h2h_snapshot_contract = (
            "# Stable H2H Snapshot Contract\n"
            "Snapshot evidence is unavailable. Do not read live H2H, match "
            "history, ratings, or bot-stat files and do not make matchup claims. "
            f"({type(exc).__name__})\n"
        )
    canonical_target_dir = get_bot_dir(target_v)
    log_file = get_logs_dir(target_v) / "crossover_io.txt"

    entry_checkpoint = read_pipeline_checkpoint() or {}
    if (
        entry_checkpoint.get("next_v") != target_v
        or entry_checkpoint.get("source_v") != parent_a_v
        or entry_checkpoint.get("parent2_v") != parent_b_v
        or entry_checkpoint.get("stage") not in {"selected", "crossover_running"}
        or int(entry_checkpoint.get("checkpoint_revision") or 0) < 1
        or not str(entry_checkpoint.get("workflow_run_id") or "").strip()
    ):
        return _crossover_projection_failure(
            "crossover_projection_contract",
            "active_checkpoint_does_not_bind_selected_crossover",
            checkpoint_stage=entry_checkpoint.get("stage"),
            checkpoint_revision=int(entry_checkpoint.get("checkpoint_revision") or 0),
        )
    workflow_root = RESULTS_DIR / "workflow"
    artifact_store = WorkerArtifactStore(workflow_root / "artifacts")
    workflow_store = WorkflowStore(workflow_root / "events.sqlite3")
    try:
        from audit_agents import (
            frozen_crossover_parent_architecture,
            resolve_crossover_parent_snapshots,
        )

        snapshot_bundle = resolve_crossover_parent_snapshots(
            parent_snapshot_receipt,
            checkpoint=entry_checkpoint,
            parent_a_v=parent_a_v,
            parent_b_v=parent_b_v,
            target_v=target_v,
            artifact_store=artifact_store,
        )
        frozen_parent_a_dir = snapshot_bundle["frozen_parent_a_dir"]
        frozen_parent_b_dir = snapshot_bundle["frozen_parent_b_dir"]
        parent_a_artifact_hash = snapshot_bundle["parent_a_artifact_hash"]
        parent_b_artifact_hash = snapshot_bundle["parent_b_artifact_hash"]
        frozen_architecture = frozen_crossover_parent_architecture(
            snapshot_bundle
        )
        architecture_policy = frozen_architecture["architecture_policy"]
        frozen_capability_context = json.loads(json.dumps(
            frozen_architecture["capability_context"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ))
    except Exception as exc:
        return _crossover_projection_failure(
            "crossover_parent_snapshot_contract",
            f"parent_snapshot_error:{type(exc).__name__}:{str(exc)[:240]}",
        )
    # Validate the fallback identity now, before any expensive LLM work.
    run_id = workflow_run_id(entry_checkpoint)
    from crossover_projection import (
        completed_crossover_projection,
        recover_crossover_projection,
    )

    recovery = recover_crossover_projection(
        entry_checkpoint=entry_checkpoint,
        target_dir=canonical_target_dir,
        parent_a_v=parent_a_v,
        parent_b_v=parent_b_v,
        target_v=target_v,
        workflow_store=workflow_store,
        artifact_store=artifact_store,
    )
    if recovery is not None:
        return recovery
    completed = completed_crossover_projection(
        checkpoint=entry_checkpoint,
        target_dir=canonical_target_dir,
        parent_a_v=parent_a_v,
        parent_b_v=parent_b_v,
        target_v=target_v,
        architecture_policy=architecture_policy,
    )
    if completed is not None:
        return completed
    try:
        entry_target_identity = _crossover_target_identity(canonical_target_dir)
    except Exception as exc:
        return _crossover_projection_failure(
            "crossover_projection_contract",
            f"target_preimage_error:{type(exc).__name__}:{str(exc)[:240]}",
        )
    preimage_artifact_hash = (
        artifact_store.capture(canonical_target_dir)
        if entry_target_identity["exists"]
        else ""
    )
    if (
        entry_target_identity["exists"]
        and preimage_artifact_hash
        != str(entry_target_identity.get("artifact_hash") or "")
    ):
        return _crossover_projection_failure(
            "crossover_projection_contract",
            "canonical_target_changed_while_capturing_preimage",
            entry_target_identity=entry_target_identity,
            captured_preimage_artifact_hash=preimage_artifact_hash,
        )
    workspace_root = RESULTS_DIR / "crossover_workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)

    architecture_retry_feedback = ""
    target_dir = None
    for attempt in range(MAX_CROSSOVER_RETRIES):
        try:
            # Every retry revalidates the same receipt/store pair.  It never
            # reopens or recaptures a mutable canonical parent directory.
            retry_snapshot_bundle = resolve_crossover_parent_snapshots(
                parent_snapshot_receipt,
                checkpoint=entry_checkpoint,
                parent_a_v=parent_a_v,
                parent_b_v=parent_b_v,
                target_v=target_v,
                artifact_store=artifact_store,
            )
            if (
                retry_snapshot_bundle["parent_a_artifact_hash"]
                != parent_a_artifact_hash
                or retry_snapshot_bundle["parent_b_artifact_hash"]
                != parent_b_artifact_hash
            ):
                raise RuntimeError("parent snapshot receipt changed between retries")
        except Exception as exc:
            return _crossover_projection_failure(
                "crossover_parent_snapshot_contract",
                "parent_snapshot_retry_validation_failed:"
                f"{type(exc).__name__}:{str(exc)[:240]}",
            )
        if target_dir is not None:
            shutil.rmtree(target_dir, ignore_errors=True)
        target_dir = tempfile.mkdtemp(
            prefix=f"v{target_v}-attempt-{attempt + 1}-",
            dir=workspace_root,
        )
        # tempfile creates the leaf; candidate copy requires a fresh path.
        target_dir = type(canonical_target_dir)(target_dir)
        target_dir.rmdir()

        # Every retry starts in a private Parent-A-derived workspace.  The
        # canonical child and checkpoint remain byte-identical until projection.
        copy_bot_tree_for_candidate(frozen_parent_a_dir, target_dir)

        try:
            from candidate_hygiene import sanitize_candidate_dir
            from workflow_profiles import get_workflow_profile
            if getattr(get_workflow_profile(), "national_execution_mode", None) != "native_tcp":
                raise RuntimeError("only native_tcp crossover is supported")
            sanitize_candidate_dir(target_dir, require_native_tcp=True)
            from bot_namespace import (
                refresh_policy_identity_documents,
                strict_lineage_parent_versions,
            )

            crossover_lineage = strict_lineage_parent_versions(
                target_v,
                parent_a_v,
                parent_b_v,
            )
            refresh_policy_identity_documents(
                target_dir,
                target_v,
                parent_versions=crossover_lineage,
            )
        except Exception as exc:
            ui.log_history(f"Crossover native TCP entry preparation failed: {exc}", "warn")
            continue

        from crossover_provenance import python_source_snapshot
        from worker_boundary import snapshot_python_files

        # Freeze the exact Parent-A-derived, system-hygiene baseline before the
        # LLM. No candidate code is auto-patched in the strict policy epoch.
        system_prepared_baseline = python_source_snapshot(target_dir)
        crossover_boundary_snapshot = snapshot_python_files(target_dir)

        crossover_renderer_inputs = {
            "parent_a_v": int(parent_a_v),
            "parent_b_v": int(parent_b_v),
            "target_v": int(target_v),
            "parent_artifacts": [
                str(parent_a_artifact_hash),
                str(parent_b_artifact_hash),
            ],
            "compatibility_receipt": compatibility_receipt,
            "capability_context": frozen_capability_context,
            "h2h_snapshot_contract": h2h_snapshot_contract,
            "architecture_policy": (
                architecture_policy if isinstance(architecture_policy, dict) else {}
            ),
            "frozen_parent_a_dir": str(frozen_parent_a_dir),
            "frozen_parent_b_dir": str(frozen_parent_b_dir),
            "retry_feedback": architecture_retry_feedback,
        }
        # Effect identity and provider receipt consume the exact same pure
        # registered renderer. No caller-assembled full prompt is admitted.
        full_attempt_prompt = _render_crossover_provider_prompt(
            crossover_renderer_inputs
        ).text
        try:
            from crossover_synthesis import (
                build_synthesis_input,
                claim_synthesis_effect,
                complete_synthesis_effect,
                ensure_synthesis_effect,
                materialize_completed_effect,
                synthesis_receipt,
            )
            from worker_workflow import WORKER_WORKFLOW_DEFINITION_VERSION
            from workflow_kernel import WorkflowBusy, WorkflowConflict

            input_snapshot_hash = artifact_store.capture(target_dir)
            effect_id, invocation_id, synthesis_input = build_synthesis_input(
                run_id=run_id,
                prompt=full_attempt_prompt,
                parent_a_v=parent_a_v,
                parent_b_v=parent_b_v,
                target_v=target_v,
                attempt=attempt + 1,
                checkpoint=entry_checkpoint,
                checkpoint_digest=_crossover_checkpoint_digest(entry_checkpoint),
                parent_a_artifact_hash=parent_a_artifact_hash,
                parent_b_artifact_hash=parent_b_artifact_hash,
                input_snapshot_hash=input_snapshot_hash,
                compatibility_receipt=compatibility_receipt,
                capability_context=frozen_capability_context,
                architecture_policy=(
                    architecture_policy
                    if isinstance(architecture_policy, dict)
                    else {}
                ),
            )
            synthesis_effect = ensure_synthesis_effect(
                store=workflow_store,
                run_id=run_id,
                effect_id=effect_id,
                input_payload=synthesis_input,
                definition_version=WORKER_WORKFLOW_DEFINITION_VERSION,
            )
        except WorkflowBusy as exc:
            shutil.rmtree(target_dir, ignore_errors=True)
            return _crossover_synthesis_in_progress(
                f"effect_prepare_busy:{str(exc)[:240]}"
            )
        except WorkflowConflict as exc:
            shutil.rmtree(target_dir, ignore_errors=True)
            return _crossover_projection_failure(
                "crossover_synthesis_effect",
                f"effect_prepare_conflict:{type(exc).__name__}:{str(exc)[:240]}",
            )
        except Exception as exc:
            shutil.rmtree(target_dir, ignore_errors=True)
            return _crossover_projection_failure(
                "crossover_synthesis_effect",
                f"effect_prepare_error:{type(exc).__name__}:{str(exc)[:240]}",
            )

        accepted_synthesis_receipt = None
        if synthesis_effect.get("status") == "completed":
            try:
                materialize_completed_effect(
                    effect=synthesis_effect,
                    workspace=target_dir,
                    artifact_store=artifact_store,
                )
                accepted_synthesis_receipt = synthesis_receipt(
                    synthesis_effect,
                    artifact_store,
                )
            except Exception as exc:
                shutil.rmtree(target_dir, ignore_errors=True)
                return _crossover_projection_failure(
                    "crossover_synthesis_replay",
                    f"completed_effect_invalid:{type(exc).__name__}:{str(exc)[:240]}",
                )
        elif synthesis_effect.get("status") in {"exhausted", "abandoned"}:
            # A provider/SDK failure terminally owns only this semantic attempt.
            # Continue to the next stable attempt id without rerunning it.
            continue
        elif synthesis_effect.get("status") == "deferred":
            shutil.rmtree(target_dir, ignore_errors=True)
            return _crossover_projection_failure(
                "crossover_synthesis_effect",
                "effect_is_deferred_pending_llm_availability_resume",
            )
        else:
            try:
                synthesis_lease = claim_synthesis_effect(
                    store=workflow_store,
                    effect_id=effect_id,
                    invocation_id=invocation_id,
                )
            except WorkflowBusy as exc:
                shutil.rmtree(target_dir, ignore_errors=True)
                return _crossover_synthesis_in_progress(
                    f"active_provider_lease:{str(exc)[:240]}",
                )
            except Exception as exc:
                shutil.rmtree(target_dir, ignore_errors=True)
                return _crossover_projection_failure(
                    "crossover_synthesis_effect",
                    f"effect_claim_error:{type(exc).__name__}:{str(exc)[:240]}",
                )

        ui.clear_io()
        ui.set_status(f"Crossover v{parent_a_v}×v{parent_b_v}→v{target_v} (Try {attempt+1})", is_working=True)
        if accepted_synthesis_receipt is None:
            try:
                resolve_crossover_parent_snapshots(
                    parent_snapshot_receipt,
                    checkpoint=entry_checkpoint,
                    parent_a_v=parent_a_v,
                    parent_b_v=parent_b_v,
                    target_v=target_v,
                    artifact_store=artifact_store,
                )
            except Exception as exc:
                shutil.rmtree(target_dir, ignore_errors=True)
                return _crossover_projection_failure(
                    "crossover_parent_snapshot_contract",
                    "parent_snapshot_pre_dispatch_validation_failed:"
                    f"{type(exc).__name__}:{str(exc)[:240]}",
                )
            try:
                from llm_query import render_llm_prompt

                crossover_role = (
                    f"CROSSOVER v{parent_a_v}×v{parent_b_v}→v{target_v} "
                    f"[{invocation_id}]"
                )
                rendered_prompt = render_llm_prompt(
                    crossover_role,
                    producer=_render_crossover_provider_prompt,
                    renderer_inputs=crossover_renderer_inputs,
                )
                await run_claude_query(
                    rendered_prompt, [], ui,
                    crossover_role,
                    log_file,
                    tools=["Bash", "Read", "Edit"],
                    allowed_write_dir={"files": [target_dir / "policy.py"]},
                    allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
                    allowed_read_dirs=[
                        frozen_parent_a_dir,
                        frozen_parent_b_dir,
                        target_dir,
                    ],
                )
            except LLMAvailabilityBlocked:
                # Persist a retryable fenced failure so the active lease cannot
                # strand resume.  The outer availability pause remains
                # attempt-neutral at the semantic crossover level.
                try:
                    workflow_store.fail_effect(
                        effect_id,
                        lease_epoch=synthesis_lease.lease_epoch,
                        error="llm_availability_blocked",
                        retryable=True,
                        causation_id=(
                            f"crossover-synthesis-availability:{effect_id}:"
                            f"{synthesis_lease.lease_epoch}"
                        ),
                    )
                except Exception:
                    # Preserve the classified availability signal even if a
                    # concurrent lease epoch already fenced this caller.
                    pass
                finally:
                    shutil.rmtree(target_dir, ignore_errors=True)
                raise
            except Exception as e:
                # A provider/SDK exception did not produce an accepted output.
                # Terminally fence this semantic attempt and advance to the
                # next stable attempt id, preserving the legacy bounded retry.
                try:
                    workflow_store.fail_effect(
                        effect_id,
                        lease_epoch=synthesis_lease.lease_epoch,
                        error=f"{type(e).__name__}: {str(e)[:2000]}",
                        retryable=False,
                        causation_id=(
                            f"crossover-synthesis-failed:{effect_id}:"
                            f"{synthesis_lease.lease_epoch}"
                        ),
                    )
                except Exception as fence_exc:
                    # A replacement epoch may already be executing this same
                    # stable attempt.  Never start attempt N+1 concurrently.
                    shutil.rmtree(target_dir, ignore_errors=True)
                    return _crossover_synthesis_in_progress(
                        "provider_failure_lost_lease:"
                        f"{type(fence_exc).__name__}:{str(fence_exc)[:200]}"
                    )
                ui.log_history(f"Crossover LLM error: {e}", "warn")
                continue

            try:
                # This must be the first operation after a successful provider
                # return: freeze the edited tree and complete through the lease
                # fence before fixes, hygiene, or deterministic gates mutate it.
                complete_synthesis_effect(
                    store=workflow_store,
                    artifact_store=artifact_store,
                    lease=synthesis_lease,
                    invocation_id=invocation_id,
                    workspace=target_dir,
                )
                synthesis_effect = workflow_store.effect(effect_id)
                accepted_synthesis_receipt = synthesis_receipt(
                    synthesis_effect,
                    artifact_store,
                )
            except WorkflowBusy as exc:
                shutil.rmtree(target_dir, ignore_errors=True)
                return _crossover_synthesis_in_progress(
                    f"provider_completion_lost_lease:{str(exc)[:240]}",
                )
            except Exception as exc:
                # Snapshot/receipt persistence is infrastructure, not a model
                # semantic failure.  Preserve the same stable effect id for an
                # at-least-once retry instead of consuming a crossover attempt.
                try:
                    workflow_store.fail_effect(
                        effect_id,
                        lease_epoch=synthesis_lease.lease_epoch,
                        error=(
                            "synthesis_completion_error:"
                            f"{type(exc).__name__}: {str(exc)[:1800]}"
                        ),
                        retryable=True,
                        causation_id=(
                            f"crossover-synthesis-completion-failed:{effect_id}:"
                            f"{synthesis_lease.lease_epoch}"
                        ),
                    )
                except Exception:
                    # A completion may already be durable even if its readback
                    # failed; the next invocation will validate and replay it.
                    pass
                shutil.rmtree(target_dir, ignore_errors=True)
                return _crossover_projection_failure(
                    "crossover_synthesis_completion",
                    f"snapshot_or_receipt_error:{type(exc).__name__}:{str(exc)[:240]}",
                )

        try:
            from candidate_hygiene import sanitize_candidate_dir
            from workflow_profiles import get_workflow_profile
            if getattr(get_workflow_profile(), "national_execution_mode", None) != "native_tcp":
                raise RuntimeError("only native_tcp crossover is supported")
            hygiene = sanitize_candidate_dir(target_dir, require_native_tcp=True)
            if hygiene.get("completed_removed") or hygiene.get("native_entry"):
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.candidate_hygiene_applied",
                        "info",
                        f"Candidate hygiene applied for crossover v{target_v}",
                        {
                            "target_v": target_v,
                            "parent_a": parent_a_v,
                            "parent_b": parent_b_v,
                            "attempt": attempt + 1,
                            **hygiene,
                        },
                    )
                except Exception:
                    pass
        except Exception as exc:
            ui.log_history(f"Crossover candidate hygiene failed: {exc}", "warn")
            continue

        # The provider receives a private directory for tool ergonomics, but
        # its semantic write authority is still exactly policy.py.  Audit the
        # raw provider output before the host rewrites identities; otherwise a
        # forged manifest or extra helper/binary could be hidden by cleanup.
        from worker_boundary import audit_worker_boundary

        crossover_boundary = audit_worker_boundary(
            target_dir,
            {
                "role": "Crossover Policy Recombiner",
                "target_files": ["policy.py"],
                "must_change_files": ["policy.py"],
            },
            crossover_boundary_snapshot,
            next_v=target_v,
        )
        if not crossover_boundary.passed:
            architecture_retry_feedback = (
                "\n\n# Previous Attempt Rejected By Artifact Write Boundary\n"
                "Rebuild from Parent A and edit exactly policy.py. Do not write "
                "identity JSON, runtime/precompute, helper modules, directories, "
                "or binary assets.\n"
                + json.dumps(
                    crossover_boundary.violations[:12],
                    indent=2,
                    ensure_ascii=False,
                )[:4000]
            )
            ui.log_history(
                "Crossover artifact write boundary failed, retrying from Parent A...",
                "warn",
            )
            continue

        try:
            from bot_namespace import (
                SYSTEM_DERIVED_IDENTITY_FILES,
                refresh_policy_identity_documents,
            )

            refresh_policy_identity_documents(
                target_dir,
                target_v,
                parent_versions=crossover_lineage,
            )
            # Provenance compares the policy to the original Parent-A/B
            # components.  Replace only its identity-document baseline with
            # the just-derived host bytes so those deterministic consequences
            # are not mistaken for LLM recombination.
            refreshed_snapshot = python_source_snapshot(target_dir)
            for relative in SYSTEM_DERIVED_IDENTITY_FILES:
                system_prepared_baseline[relative] = refreshed_snapshot[relative]
        except Exception as exc:
            ui.log_history(
                f"Crossover policy identity refresh failed: {exc}",
                "warn",
            )
            continue

        compile_errors = verify_code(target_dir)
        if compile_errors:
            ui.log_history("Crossover compile error, retrying...", "warn")
            continue

        import_errors = run_import_contract_test(target_dir)
        if import_errors:
            try:
                from system_log import log_system_event
                log_system_event(
                    "pipeline.crossover_import_contract_failed", "error",
                    f"Crossover v{target_v} import contract failed on attempt {attempt+1}: "
                    f"{import_errors[0].get('module')} {import_errors[0].get('exception')}: "
                    f"{import_errors[0].get('message')}",
                    {"target_v": target_v, "parent_a": parent_a_v, "parent_b": parent_b_v,
                     "attempt": attempt + 1, "errors": import_errors[:3]},
                )
            except Exception:
                pass
            ui.log_history("Crossover runtime import contract failed, retrying...", "warn")
            continue

        from workflow_profiles import get_workflow_profile

        from national_native import run_native_tcp_smoke

        smoke_report = await run_native_tcp_smoke(
            target_dir,
            source_v=parent_a_v,
            opponent_token=frozen_parent_a_dir,
            hands=1,
        )
        smoke_errors = (
            []
            if smoke_report.get("passed") is True
            else list(smoke_report.get("issues") or ["native_crossover_smoke_failed"])
        )
        if smoke_errors:
            ui.log_history("Crossover smoke test failed, retrying...", "warn")
            continue

        from code_verification import check_code_size

        _total_lines, oversized_files = check_code_size(
            target_dir,
            source_dir=frozen_parent_a_dir,
        )
        if oversized_files:
            architecture_retry_feedback = (
                "\n\n# Previous Attempt Rejected By Code Size Contract\n"
                "Rebuild from Parent A and keep every file within the exact "
                "source-relative limit below. Do not postpone this debt to Master.\n"
                + json.dumps(
                    [
                        {"file": name, "lines": lines, "limit": limit}
                        for name, lines, limit in oversized_files[:12]
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
            try:
                from system_log import log_system_event

                log_system_event(
                    "pipeline.crossover_code_size_rejected",
                    "warn",
                    f"Crossover v{target_v} attempt {attempt + 1} exceeded code-size limits",
                    {
                        "target_v": target_v,
                        "parent_a": parent_a_v,
                        "parent_b": parent_b_v,
                        "attempt": attempt + 1,
                        "total_lines": _total_lines,
                        "oversized_files": oversized_files[:12],
                    },
                )
            except Exception:
                pass
            ui.log_history(
                "Crossover code-size contract failed, retrying from Parent A baseline...",
                "warn",
            )
            continue

        from crossover_provenance import (
            validate_crossover_recombination_provenance,
        )

        provenance_issues = validate_crossover_recombination_provenance(
            system_prepared_baseline,
            frozen_parent_b_dir,
            target_dir,
        )
        if provenance_issues:
            architecture_retry_feedback = (
                "\n\n# Previous Attempt Rejected By Crossover Provenance Contract\n"
                "This stage is pure recombination. Every strategic diff must "
                "contain a traceable Parent-B component; independent threshold, "
                "heuristic, deletion, or novel-file mutations belong to the later "
                "Master/Worker stage. Rebuild from Parent A and either import an "
                "actual Parent-B component or leave Parent A unchanged.\n"
                + json.dumps(
                    provenance_issues[:12],
                    indent=2,
                    ensure_ascii=False,
                )[:5000]
            )
            try:
                from system_log import log_system_event

                log_system_event(
                    "pipeline.crossover_provenance_rejected",
                    "warn",
                    f"Crossover v{target_v} attempt {attempt + 1} contained an independent mutation",
                    {
                        "target_v": target_v,
                        "parent_a": parent_a_v,
                        "parent_b": parent_b_v,
                        "attempt": attempt + 1,
                        "issues": provenance_issues[:12],
                    },
                )
            except Exception:
                pass
            ui.log_history(
                "Crossover provenance contract failed, retrying from Parent A baseline...",
                "warn",
            )
            continue

        try:
            from national_position_contract import detect_position_semantics_errors

            position_errors = detect_position_semantics_errors(target_dir)
        except Exception as exc:
            position_errors = [
                "position_contract_check_error:"
                f"{type(exc).__name__}:{str(exc)[:200]}"
            ]
        if position_errors:
            architecture_retry_feedback = (
                "\n\n# Previous Attempt Rejected By National Position Contract\n"
                "Rebuild from parent A and correct these hard protocol errors.\n"
                + json.dumps(position_errors[:10], indent=2, ensure_ascii=False)
            )
            try:
                from system_log import log_system_event

                log_system_event(
                    "pipeline.crossover_position_contract_rejected",
                    "warn",
                    f"Crossover v{target_v} attempt {attempt + 1} failed position contract",
                    {
                        "target_v": target_v,
                        "parent_a": parent_a_v,
                        "parent_b": parent_b_v,
                        "attempt": attempt + 1,
                        "errors": position_errors[:10],
                    },
                )
            except Exception:
                pass
            ui.log_history(
                "Crossover national position contract failed, retrying from parent A baseline...",
                "warn",
            )
            continue

        if isinstance(architecture_policy, dict):
            try:
                from runtime_architecture_policy import (
                    ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
                    evaluate_architecture_transition,
                )

                transition = evaluate_architecture_transition(
                    frozen_parent_a_dir,
                    target_dir,
                    expected_policy=architecture_policy,
                    evaluation_phase=ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
                )
            except Exception as exc:
                transition = {
                    "ok": False,
                    "conclusive": False,
                    "outcome": "infrastructure_failure",
                    "failure_class": "infrastructure",
                    "infrastructure_failures": [{
                        "component": "runtime_architecture_policy",
                        "failure_class": "internal_infrastructure",
                        "issues": [
                            f"transition_exception:{type(exc).__name__}:{str(exc)[:200]}"
                        ],
                    }],
                    "policy_identity_errors": [
                        f"transition_exception:{type(exc).__name__}:{str(exc)[:200]}"
                    ],
                    "regressions": [],
                    "unresolved_focus_checks": [],
                }
            if transition.get("outcome") == "infrastructure_failure":
                failures = transition.get("infrastructure_failures") or [{
                    "component": "national_runtime_probe",
                    "failure_class": "probe_infrastructure",
                    "issues": ["preplan architecture assessment was inconclusive"],
                }]
                try:
                    from system_log import log_system_event

                    log_system_event(
                        "pipeline.crossover_architecture_infrastructure",
                        "error",
                        f"Crossover v{target_v} preplan architecture probe was inconclusive",
                        {
                            "target_v": target_v,
                            "parent_a": parent_a_v,
                            "parent_b": parent_b_v,
                            "attempt": attempt + 1,
                            "infrastructure_failures": failures,
                        },
                    )
                except Exception:
                    pass
                failure = {
                    "success": False,
                    "failure_class": "infrastructure",
                    "outcome": "infrastructure_failure",
                    "component": str(
                        (failures[0] or {}).get("component")
                        if isinstance(failures[0], dict)
                        else "national_runtime_probe"
                    ),
                    "infrastructure_failures": failures,
                    "transition": transition,
                }
                shutil.rmtree(target_dir, ignore_errors=True)
                return failure
            if not transition.get("ok"):
                candidate_capabilities = transition.get("candidate_capabilities") or {}
                candidate_checks = candidate_capabilities.get("checks_by_id") or {}
                blocking_ids = {
                    str(check_id)
                    for check_id in transition.get("unresolved_focus_checks") or []
                }
                blocking_ids.update(
                    str(item.get("check_id") or "")
                    for item in transition.get("runtime_floor_failures") or []
                    if item.get("check_id")
                )
                blocking_ids.update(
                    str(item.get("check_id") or "")
                    for item in transition.get("regressions") or []
                    if item.get("check_id")
                )
                blocking_check_details = {}
                for check_id in sorted(blocking_ids):
                    check = candidate_checks.get(check_id) or {}
                    evidence = check.get("evidence") or {}
                    detail = {
                        "guidance": check.get("guidance") or "",
                        "locations": list(evidence.get("locations") or [])[:8],
                        "facts": evidence.get("facts") or {},
                    }
                    if check_id == "killable_decision_runtime":
                        runtime_evidence = (
                            candidate_capabilities.get("decision_runtime_evidence") or {}
                        )
                        detail["safety_issues"] = list(
                            runtime_evidence.get("safety_issues") or []
                        )[:8]
                    blocking_check_details[check_id] = detail
                architecture_retry_feedback = (
                    "\n\n# Previous Attempt Rejected By Runtime Architecture Gate\n"
                    "Rebuild from parent A and correct every blocking item below. "
                    "Do not merely add labels. Items under deferred_to_master are "
                    "not crossover work and must remain deferred.\n"
                    + json.dumps(
                        {
                            "policy_identity_errors": transition.get("policy_identity_errors") or [],
                            "regressions": transition.get("regressions") or [],
                            "runtime_floor_failures": transition.get("runtime_floor_failures") or [],
                            "unresolved_focus_checks": transition.get("unresolved_focus_checks") or [],
                            "blocking_check_details": blocking_check_details,
                            "deferred_to_master": (
                                transition.get("deferred_unresolved_focus_checks") or []
                            ),
                        },
                        indent=2,
                        ensure_ascii=False,
                    )[:5000]
                )
                try:
                    from system_log import log_system_event

                    log_system_event(
                        "pipeline.crossover_architecture_rejected",
                        "warn",
                        f"Crossover v{target_v} attempt {attempt + 1} failed runtime architecture policy",
                        {
                            "target_v": target_v,
                            "parent_a": parent_a_v,
                            "parent_b": parent_b_v,
                            "attempt": attempt + 1,
                            "evaluation_phase": transition.get("evaluation_phase"),
                            "regressions": transition.get("regressions") or [],
                            "runtime_floor_failures": transition.get("runtime_floor_failures") or [],
                            "unresolved_focus_checks": transition.get("unresolved_focus_checks") or [],
                            "blocking_check_details": blocking_check_details,
                            "deferred_to_master": (
                                transition.get("deferred_unresolved_focus_checks") or []
                            ),
                            "policy_identity_errors": transition.get("policy_identity_errors") or [],
                        },
                    )
                except Exception:
                    pass
                ui.log_history(
                    "Crossover runtime architecture policy failed, retrying from parent A baseline...",
                    "warn",
                )
                continue
            deferred_checks = list(
                transition.get("deferred_unresolved_focus_checks") or []
            )
            if deferred_checks:
                try:
                    from system_log import log_system_event

                    log_system_event(
                        "pipeline.crossover_architecture_debt_deferred",
                        "info",
                        f"Crossover v{target_v} baseline accepted with downstream architecture debt",
                        {
                            "target_v": target_v,
                            "parent_a": parent_a_v,
                            "parent_b": parent_b_v,
                            "attempt": attempt + 1,
                            "evaluation_phase": transition.get("evaluation_phase"),
                            "deferred_to_master": deferred_checks,
                        },
                    )
                except Exception:
                    pass

        # LOG GAP FIX (2026-06-30): record which files the crossover LLM actually
        # changed vs parent_a, so the modification is auditable (parity with the
        # worker_files_reset event on the evolve path).
        try:
            changed = []
            if frozen_parent_a_dir.exists():
                src_files = {f.name for f in frozen_parent_a_dir.glob("*.py")}
                for f in target_dir.glob("*.py"):
                    src_f = frozen_parent_a_dir / f.name
                    if f.name not in src_files:
                        changed.append(f.name + " (new)")
                    elif src_f.exists() and f.read_text() != src_f.read_text():
                        changed.append(f.name + " (modified)")
            from system_log import log_system_event
            log_system_event(
                "pipeline.crossover_files_changed", "info",
                f"Crossover v{target_v} (v{parent_a_v}×v{parent_b_v}): {len(changed)} "
                f"file(s) changed vs parent v{parent_a_v} (attempt {attempt+1})",
                {"target_v": target_v, "parent_a": parent_a_v, "parent_b": parent_b_v,
                 "attempt": attempt + 1, "changed_files": changed[:20]},
            )
        except Exception:
            pass

        projection = _project_crossover_candidate(
            workspace=target_dir,
            target_dir=canonical_target_dir,
            parent_a_v=parent_a_v,
            parent_b_v=parent_b_v,
            target_v=target_v,
            attempt=attempt,
            compatibility=compatibility_receipt,
            architecture_policy=architecture_policy,
            synthesis_receipt=accepted_synthesis_receipt,
            entry_checkpoint=entry_checkpoint,
            entry_target_identity=entry_target_identity,
            preimage_artifact_hash=preimage_artifact_hash,
            workflow_store=workflow_store,
            artifact_store=artifact_store,
        )
        shutil.rmtree(target_dir, ignore_errors=True)
        return projection

    if target_dir is not None:
        shutil.rmtree(target_dir, ignore_errors=True)
    return False
