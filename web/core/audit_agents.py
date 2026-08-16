"""LLM audit agents for the evolution pipeline.

Each audit function follows the same pattern:
1. Load prompt template from prompts/ directory
2. Build context data from system state
3. Call run_claude_query() for LLM analysis
4. Parse + validate output against Pydantic schema
5. Return validated dict or safe default on failure

Audit LLM infrastructure/parse failures return safe defaults, but validated audit
results can be used as hard gates by their callers. For example Master plan
audit rejection, crossover compatibility rejection, and high-confidence
precommit semantic regression can block their respective stages.
"""

import json
import hashlib
import logging
import difflib
import asyncio
import stat
from pathlib import Path

from bot_namespace import (
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
    bot_tag,
    parse_bot_version,
)
from evolution_infra import (
    PROMPTS_DIR, RESULTS_DIR,
    get_bot_dir, get_logs_dir,
    run_claude_query, parse_json_output, substitute_template,
    _target_rel,
)
from output_schema import validate_agent_output
from system_log import log_system_event
from llm_failure import is_llm_infra_error
from llm_availability import LLMAvailabilityBlocked
from worker_boundary import is_binary_artifact_path, read_regular_file_bytes

log = logging.getLogger("pok.audit")


_FENCED_WORKER_OUTPUT_AUTHORITY = object()
_FENCED_WORKER_OUTPUT_KEYS = frozenset({
    "schema_version",
    "kind",
    "next_v",
    "source_v",
    "worker_id",
    "workflow_run_id",
    "envelope_digest",
    "effect_id",
    "lease_epoch",
    "attempt",
    "task_digest",
    "worker_dispatch_receipt_digest",
    "output_sha256",
    "output_bytes",
    "output_excerpt",
    "output_excerpt_sha256",
    "output_excerpt_mode",
    "binding_digest",
})


class WorkerCoTEvidenceError(RuntimeError):
    """The CoT audit was not given the current fenced Worker output."""


class FencedWorkerOutput:
    """In-process authority token for one exact Worker provider result."""

    __slots__ = ("payload", "_authority")

    def __init__(self, payload, authority):
        self.payload = payload
        self._authority = authority


def _worker_output_payload_digest(payload):
    from bot_artifact import canonical_digest

    return canonical_digest({
        key: value for key, value in payload.items() if key != "binding_digest"
    })


def _validate_fenced_worker_output_payload(
    payload,
    *,
    task,
    worker_id,
    next_v,
    source_v,
):
    from bot_artifact import canonical_digest

    if not isinstance(payload, dict) or set(payload) != _FENCED_WORKER_OUTPUT_KEYS:
        raise WorkerCoTEvidenceError("worker_output_evidence_fields_invalid")
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "fenced-worker-provider-output-v1"
        or type(payload.get("next_v")) is not int
        or payload.get("next_v") != int(next_v)
        or type(payload.get("source_v")) is not int
        or payload.get("source_v") != int(source_v)
        or payload.get("worker_id") != str(worker_id)
        or not isinstance(payload.get("workflow_run_id"), str)
        or not payload.get("workflow_run_id")
        or not isinstance(payload.get("effect_id"), str)
        or not payload.get("effect_id")
        or type(payload.get("lease_epoch")) is not int
        or payload.get("lease_epoch") <= 0
        or type(payload.get("attempt")) is not int
        or payload.get("attempt") <= 0
        or payload.get("task_digest") != canonical_digest(task)
        or payload.get("output_excerpt_mode") != "utf8_tail_5000_chars"
        or not isinstance(payload.get("output_excerpt"), str)
        or len(payload.get("output_excerpt")) > 5000
        or type(payload.get("output_bytes")) is not int
        or payload.get("output_bytes") < len(
            payload.get("output_excerpt").encode("utf-8")
        )
    ):
        raise WorkerCoTEvidenceError("worker_output_evidence_subject_invalid")
    for field in (
        "envelope_digest",
        "worker_dispatch_receipt_digest",
        "output_sha256",
        "output_excerpt_sha256",
        "binding_digest",
    ):
        value = payload.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise WorkerCoTEvidenceError(
                f"worker_output_evidence_{field}_invalid"
            )
    if payload.get("output_excerpt_sha256") != hashlib.sha256(
        payload["output_excerpt"].encode("utf-8")
    ).hexdigest():
        raise WorkerCoTEvidenceError("worker_output_excerpt_digest_mismatch")
    if payload.get("binding_digest") != _worker_output_payload_digest(payload):
        raise WorkerCoTEvidenceError("worker_output_binding_digest_mismatch")
    return payload


def bind_fenced_worker_output(
    *,
    task,
    worker_id,
    next_v,
    source_v,
    worker_effect_identity,
    attempt,
    dispatch_receipt_digest,
    output,
):
    """Bind the current provider result to its Worker effect lease."""

    from bot_artifact import canonical_digest

    identity = worker_effect_identity
    if not isinstance(identity, dict) or set(identity) != {
        "workflow_run_id", "envelope_digest", "effect_id", "lease_epoch",
    }:
        raise WorkerCoTEvidenceError("worker_effect_identity_invalid")
    text = str(output or "")
    encoded = text.encode("utf-8")
    excerpt = text[-5000:]
    payload = {
        "schema_version": 1,
        "kind": "fenced-worker-provider-output-v1",
        "next_v": int(next_v),
        "source_v": int(source_v),
        "worker_id": str(worker_id),
        "workflow_run_id": str(identity.get("workflow_run_id") or ""),
        "envelope_digest": str(identity.get("envelope_digest") or ""),
        "effect_id": str(identity.get("effect_id") or ""),
        "lease_epoch": identity.get("lease_epoch"),
        "attempt": int(attempt),
        "task_digest": canonical_digest(task),
        "worker_dispatch_receipt_digest": str(dispatch_receipt_digest or ""),
        "output_sha256": hashlib.sha256(encoded).hexdigest(),
        "output_bytes": len(encoded),
        "output_excerpt": excerpt,
        "output_excerpt_sha256": hashlib.sha256(
            excerpt.encode("utf-8")
        ).hexdigest(),
        "output_excerpt_mode": "utf8_tail_5000_chars",
    }
    payload["binding_digest"] = _worker_output_payload_digest(payload)
    _validate_fenced_worker_output_payload(
        payload,
        task=task,
        worker_id=worker_id,
        next_v=next_v,
        source_v=source_v,
    )
    return FencedWorkerOutput(payload, _FENCED_WORKER_OUTPUT_AUTHORITY)


def _open_fenced_worker_output(
    evidence,
    *,
    task,
    worker_id,
    next_v,
    source_v,
):
    if (
        not isinstance(evidence, FencedWorkerOutput)
        or evidence._authority is not _FENCED_WORKER_OUTPUT_AUTHORITY
    ):
        raise WorkerCoTEvidenceError("fenced_worker_output_authority_missing")
    return _validate_fenced_worker_output_payload(
        evidence.payload,
        task=task,
        worker_id=worker_id,
        next_v=next_v,
        source_v=source_v,
    )


def _render_master_plan_audit_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "source_v", "next_v", "master_plan", "recent_commits",
        "direction_audit", "h2h_snapshot_contract", "recent_directions",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Master plan audit renderer input contract mismatch")
    source_v = int(inputs["source_v"])
    next_v = int(inputs["next_v"])
    master_plan = inputs["master_plan"]
    if not isinstance(master_plan, dict):
        raise ValueError("Master plan audit input must be an object")
    recent_commits = str(inputs["recent_commits"])
    recent_directions = str(inputs["recent_directions"])
    template = (
        Path(__file__).resolve().parent / "prompts" / "master_plan_audit.md"
    ).read_text(encoding="utf-8")
    text = substitute_template(template, {
        "master_plan": json.dumps(master_plan, indent=2, ensure_ascii=False),
        "recent_commits": (
            recent_commits
            or "No strict published completion commits are available."
        ),
        "direction_audit": str(inputs["direction_audit"]),
        "source_v": str(source_v),
        "next_v": str(next_v),
        "h2h_snapshot_contract": str(inputs["h2h_snapshot_contract"]),
        "recent_directions": (
            recent_directions
            or "No recent direction ledger is available."
        ),
        "branch_from_note": (
            f"This generation evolves FROM v{source_v}. The source ancestor is "
            "decided automatically by the system in prepare_generation; the Master "
            "plan MUST NOT set 'branch_from' (it is a dead, rejected field). Only "
            "flag a 'data staleness' problem if the plan's analysis references a "
            f"version OTHER than v{source_v} as if it were the evolution base. Do NOT "
            f"reject a plan just because it fixes bugs in v{source_v} that happen to "
            f"already be fixed in a later version — evolution starts from v{source_v}, "
            "not the latest version."
        ),
    })

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="compiled_plan_completion_history",
        evidence_provenance={
            "source_v": source_v,
            "next_v": next_v,
            "plan_digest": hashlib.sha256(
                json.dumps(
                    master_plan,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "history_digest": hashlib.sha256(
                recent_commits.encode("utf-8")
            ).hexdigest(),
            "direction_audit_digest": hashlib.sha256(
                str(inputs["direction_audit"]).encode("utf-8")
            ).hexdigest(),
            "h2h_snapshot_digest": hashlib.sha256(
                str(inputs["h2h_snapshot_contract"]).encode("utf-8")
            ).hexdigest(),
            "recent_directions_digest": hashlib.sha256(
                recent_directions.encode("utf-8")
            ).hexdigest(),
        },
    )


def _recent_directions_for_audit() -> str:
    """System-rendered cross-generation direction ledger for the plan audit.

    2026-08-16 audit: novelty was scored against PUBLISHED commit bodies only,
    so recycled directions across abandoned generations stamped "novel" every
    time. This ledger covers published AND abandoned attempts (extracted from
    strict master logs; see recent_directions.py)."""
    try:
        from recent_directions import published_versions, recent_change_symbols

        rows = recent_change_symbols(10)
        if not rows:
            return ""
        published = published_versions()
        return "; ".join(
            f"v{v} {sym}"
            + (" published" if v in published else " not-published")
            for v, sym in rows
        )
    except Exception:
        return ""


def _render_worker_cot_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "task", "worker_role", "worker_task", "worker_output_evidence",
        "code_diff", "diff_metadata",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Worker CoT renderer input contract mismatch")
    task = inputs["task"]
    if not isinstance(task, dict):
        raise ValueError("Worker CoT task must be an object")
    evidence = inputs["worker_output_evidence"]
    if not isinstance(evidence, dict) or set(evidence) != _FENCED_WORKER_OUTPUT_KEYS:
        raise ValueError("Worker CoT output evidence contract mismatch")
    # Renderer replay revalidates the self-contained receipt. The private
    # in-process authority token was consumed before renderer invocation.
    _validate_fenced_worker_output_payload(
        evidence,
        task=task,
        worker_id=evidence.get("worker_id"),
        next_v=evidence.get("next_v"),
        source_v=evidence.get("source_v"),
    )
    worker_output = evidence["output_excerpt"]
    code_diff = str(inputs["code_diff"])
    template = (
        Path(__file__).resolve().parent / "prompts" / "worker_cot_check.md"
    ).read_text(encoding="utf-8")
    text = substitute_template(template, {
        "worker_role": str(inputs["worker_role"]),
        "worker_task": str(inputs["worker_task"])[:2000],
        "worker_output": worker_output[-3000:],
        "code_diff": code_diff,
        "diff_metadata": str(inputs["diff_metadata"]),
    })

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="worker_output_diff",
        evidence_provenance={
            "task_digest": hashlib.sha256(
                json.dumps(
                    task,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "diff_digest": hashlib.sha256(code_diff.encode("utf-8")).hexdigest(),
            "worker_output_digest": evidence["output_sha256"],
            "worker_output_binding_digest": evidence["binding_digest"],
            "worker_effect_id": evidence["effect_id"],
            "worker_lease_epoch": evidence["lease_epoch"],
            "worker_dispatch_receipt_digest": evidence[
                "worker_dispatch_receipt_digest"
            ],
            "worker_role_digest": hashlib.sha256(
                str(inputs["worker_role"]).encode("utf-8")
            ).hexdigest(),
            "worker_task_digest": hashlib.sha256(
                str(inputs["worker_task"]).encode("utf-8")
            ).hexdigest(),
            "diff_metadata_digest": hashlib.sha256(
                str(inputs["diff_metadata"]).encode("utf-8")
            ).hexdigest(),
        },
    )


def _render_degeneration_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {"source_v", "recent_commits", "strategy_changes", "rating_curve"}
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Degeneration renderer input contract mismatch")
    recent_commits = str(inputs["recent_commits"])
    rating_curve = str(inputs["rating_curve"])
    template = (
        Path(__file__).resolve().parent / "prompts" / "degeneration_diagnosis.md"
    ).read_text(
        encoding="utf-8"
    )
    text = substitute_template(template, {
        "generation_history": recent_commits[:3000],
        "rating_curve": rating_curve[:2000],
        "h2h_changes": (
            "No per-opponent H2H delta rows were supplied to this advisory "
            "role; opponent-specific attribution is unknown."
        ),
        "strategy_changes": str(inputs["strategy_changes"])[:3000],
    })

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="frozen_degeneration_window",
        evidence_provenance={
            "source_v": int(inputs["source_v"]),
            "history_digest": hashlib.sha256(
                recent_commits.encode("utf-8")
            ).hexdigest(),
            "rating_digest": hashlib.sha256(
                rating_curve.encode("utf-8")
            ).hexdigest(),
            "strategy_changes_digest": hashlib.sha256(
                str(inputs["strategy_changes"]).encode("utf-8")
            ).hexdigest(),
        },
    )


def _render_crossover_compat_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "parent_a_v", "parent_b_v", "parent_a_code", "parent_b_code",
        "parent_a_rating", "parent_b_rating", "h2h_context",
        "architecture_context", "parent_snapshot_receipt",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Crossover compatibility renderer input contract mismatch")
    parent_a_v = int(inputs["parent_a_v"])
    parent_b_v = int(inputs["parent_b_v"])
    parent_a_code = inputs["parent_a_code"]
    parent_b_code = inputs["parent_b_code"]
    parent_snapshot_receipt = inputs["parent_snapshot_receipt"]
    if not isinstance(parent_a_code, dict) or not isinstance(parent_b_code, dict):
        raise ValueError("Crossover parent code inputs must be objects")
    if not isinstance(parent_snapshot_receipt, dict):
        raise ValueError("Crossover parent snapshot receipt must be an object")
    template = (
        Path(__file__).resolve().parent / "prompts" / "crossover_compatibility.md"
    ).read_text(
        encoding="utf-8"
    )
    text = substitute_template(template, {
        "parent_a_version": str(parent_a_v),
        "parent_b_version": str(parent_b_v),
        "parent_a_code": json.dumps(
            parent_a_code, indent=2, ensure_ascii=False
        )[:5000],
        "parent_b_code": json.dumps(
            parent_b_code, indent=2, ensure_ascii=False
        )[:5000],
        "parent_a_rating": str(inputs["parent_a_rating"]),
        "parent_b_rating": str(inputs["parent_b_rating"]),
        "h2h_a_vs_b": str(inputs["h2h_context"]),
        "architecture_context": json.dumps(
            inputs["architecture_context"], indent=2, ensure_ascii=False
        )[:8000],
    })
    # This advisory crossover-audit renderer cannot alter a candidate or
    # publish an effect by itself, but it still reasons about active strict
    # policy bytes.  Bind the same current system-owned national runtime
    # contract as every other provider-facing role so it cannot recommend an
    # obsolete protocol/timing/ABI premise.
    from strategy_reference_pack import current_strict_runtime_prompt_overlay

    text += "\n\n" + current_strict_runtime_prompt_overlay()

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="crossover_parent_compatibility",
        evidence_provenance={
            "parent_a_v": parent_a_v,
            "parent_b_v": parent_b_v,
            "parent_code_digest": hashlib.sha256(
                json.dumps(
                    {"a": parent_a_code, "b": parent_b_code},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "rating_context_digest": hashlib.sha256(
                json.dumps(
                    {
                        "parent_a_rating": inputs["parent_a_rating"],
                        "parent_b_rating": inputs["parent_b_rating"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "h2h_context_digest": hashlib.sha256(
                str(inputs["h2h_context"]).encode("utf-8")
            ).hexdigest(),
            "architecture_context_digest": hashlib.sha256(
                json.dumps(
                    inputs["architecture_context"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "parent_snapshot_receipt_digest": str(
                parent_snapshot_receipt.get("receipt_digest") or ""
            ),
        },
    )


class CrossoverParentSnapshotError(RuntimeError):
    """The selected published parents could not be frozen without ambiguity."""


_CROSSOVER_PARENT_SNAPSHOT_RECEIPT_KEYS = frozenset({
    "schema_version",
    "kind",
    "target_v",
    "parent_a_v",
    "parent_b_v",
    "workflow_run_id",
    "checkpoint_revision",
    "checkpoint_stage",
    "checkpoint_digest",
    "epoch_binding_digest",
    "parents",
    "receipt_digest",
})
_CROSSOVER_PARENT_SNAPSHOT_ENTRY_KEYS = frozenset({
    "role",
    "version",
    "snapshot_artifact_hash",
    "publication_identity",
    "publication_identity_digest",
})


def _crossover_snapshot_fail(issue):
    raise CrossoverParentSnapshotError(str(issue))


def _crossover_snapshot_digest(payload):
    from bot_artifact import canonical_digest

    return canonical_digest(payload)


def _crossover_checkpoint_subject_errors(
    checkpoint,
    *,
    parent_a_v,
    parent_b_v,
    target_v,
):
    from checkpoint_schema import checkpoint_epoch_errors

    errors = list(checkpoint_epoch_errors(checkpoint))
    if not isinstance(checkpoint, dict):
        return errors or ["crossover_parent_checkpoint_missing"]
    if checkpoint.get("next_v") != int(target_v):
        errors.append("crossover_parent_checkpoint_target_mismatch")
    if checkpoint.get("source_v") != int(parent_a_v):
        errors.append("crossover_parent_checkpoint_parent_a_mismatch")
    if checkpoint.get("parent2_v") != int(parent_b_v):
        errors.append("crossover_parent_checkpoint_parent_b_mismatch")
    if checkpoint.get("stage") not in {"selected", "crossover_running"}:
        errors.append("crossover_parent_checkpoint_stage_invalid")
    if (
        type(checkpoint.get("checkpoint_revision")) is not int
        or checkpoint.get("checkpoint_revision") < 1
    ):
        errors.append("crossover_parent_checkpoint_revision_invalid")
    if not str(checkpoint.get("workflow_run_id") or "").strip():
        errors.append("crossover_parent_checkpoint_workflow_missing")
    return list(dict.fromkeys(errors))


def resolve_crossover_parent_snapshots(
    receipt,
    *,
    checkpoint,
    parent_a_v,
    parent_b_v,
    target_v,
    artifact_store=None,
):
    """Resolve an exact compatibility receipt to immutable Worker snapshots.

    This deliberately does not reopen ``bots/national_v*``.  Once compatibility
    capture succeeds, synthesis and all retries consume only these two
    content-addressed trees.
    """

    from crossover_projection import checkpoint_digest
    from worker_workflow import WorkerArtifactStore

    subject_errors = _crossover_checkpoint_subject_errors(
        checkpoint,
        parent_a_v=parent_a_v,
        parent_b_v=parent_b_v,
        target_v=target_v,
    )
    if subject_errors:
        _crossover_snapshot_fail(subject_errors[0])
    if not isinstance(receipt, dict):
        _crossover_snapshot_fail("crossover_parent_snapshot_receipt_missing")
    if set(receipt) != _CROSSOVER_PARENT_SNAPSHOT_RECEIPT_KEYS:
        _crossover_snapshot_fail("crossover_parent_snapshot_receipt_fields_mismatch")
    exact_checkpoint_binding = bool(
        receipt.get("checkpoint_revision")
        == checkpoint.get("checkpoint_revision")
        and receipt.get("checkpoint_stage") == checkpoint.get("stage")
        and receipt.get("checkpoint_digest") == checkpoint_digest(checkpoint)
    )
    projected_checkpoint_binding = False
    if not exact_checkpoint_binding:
        from workflow_kernel import content_digest

        crossover = (
            ((checkpoint.get("audit_context") or {}).get("crossover") or {})
            if isinstance(checkpoint.get("audit_context"), dict)
            else {}
        )
        projection = (
            crossover.get("projection") if isinstance(crossover, dict) else None
        )
        projection_body = (
            {key: value for key, value in projection.items() if key != "projection_id"}
            if isinstance(projection, dict)
            else {}
        )
        projected_checkpoint_binding = bool(
            isinstance(projection, dict)
            and projection.get("schema_version") == 1
            and projection.get("workflow_run_id")
            == checkpoint.get("workflow_run_id")
            and projection.get("parent_a_v") == int(parent_a_v)
            and projection.get("parent_b_v") == int(parent_b_v)
            and projection.get("target_v") == int(target_v)
            and projection.get("expected_checkpoint_digest")
            == receipt.get("checkpoint_digest")
            and projection.get("expected_checkpoint_revision")
            == receipt.get("checkpoint_revision")
            and projection.get("expected_checkpoint_stage")
            == receipt.get("checkpoint_stage")
            and int(checkpoint.get("checkpoint_revision") or -1)
            >= int(projection.get("committed_revision") or 0)
            and projection.get("projection_id") == content_digest(projection_body)
            and (
                ((projection.get("crossover_semantics") or {}).get(
                    "compatibility"
                ) or {}).get("parent_snapshot_receipt")
                == receipt
            )
        )
    if (
        receipt.get("schema_version") != 1
        or receipt.get("kind") != "crossover-published-parent-snapshots-v1"
        or receipt.get("target_v") != int(target_v)
        or receipt.get("parent_a_v") != int(parent_a_v)
        or receipt.get("parent_b_v") != int(parent_b_v)
        or receipt.get("workflow_run_id")
        != checkpoint.get("workflow_run_id")
        or not (exact_checkpoint_binding or projected_checkpoint_binding)
        or receipt.get("epoch_binding_digest")
        != ((checkpoint.get("epoch_binding") or {}).get("binding_digest"))
    ):
        _crossover_snapshot_fail("crossover_parent_snapshot_receipt_subject_mismatch")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    if receipt.get("receipt_digest") != _crossover_snapshot_digest(unsigned):
        _crossover_snapshot_fail("crossover_parent_snapshot_receipt_digest_mismatch")

    identities = (
        (checkpoint.get("epoch_binding") or {}).get("published_parent_identities")
    )
    parents = receipt.get("parents")
    if (
        not isinstance(identities, list)
        or len(identities) != 2
        or not isinstance(parents, list)
        or len(parents) != 2
    ):
        _crossover_snapshot_fail("crossover_parent_snapshot_identity_set_invalid")
    expected = (
        ("parent_a", int(parent_a_v), identities[0]),
        ("parent_b", int(parent_b_v), identities[1]),
    )
    snapshot_hashes = []
    for item, (role, version, identity) in zip(parents, expected):
        if not isinstance(item, dict) or set(item) != _CROSSOVER_PARENT_SNAPSHOT_ENTRY_KEYS:
            _crossover_snapshot_fail("crossover_parent_snapshot_entry_fields_mismatch")
        if (
            item.get("role") != role
            or item.get("version") != version
            or item.get("publication_identity") != identity
            or item.get("publication_identity_digest")
            != _crossover_snapshot_digest(identity)
            or item.get("snapshot_artifact_hash")
            != identity.get("tag_artifact_hash")
        ):
            _crossover_snapshot_fail(
                f"crossover_parent_snapshot_{role}_identity_mismatch"
            )
        snapshot_hashes.append(str(item["snapshot_artifact_hash"]))

    store = artifact_store or WorkerArtifactStore(
        RESULTS_DIR / "workflow" / "artifacts"
    )
    try:
        parent_a_dir = store.path_for(snapshot_hashes[0])
        parent_b_dir = store.path_for(snapshot_hashes[1])
    except Exception as exc:
        _crossover_snapshot_fail(
            "crossover_parent_snapshot_store_mismatch:"
            f"{type(exc).__name__}:{str(exc)[:240]}"
        )
    return {
        "receipt": receipt,
        "parent_a_artifact_hash": snapshot_hashes[0],
        "parent_b_artifact_hash": snapshot_hashes[1],
        "frozen_parent_a_dir": parent_a_dir,
        "frozen_parent_b_dir": parent_b_dir,
    }


def capture_crossover_parent_snapshots(
    parent_a_v,
    parent_b_v,
    target_v,
    *,
    checkpoint,
    artifact_store=None,
    checkpoint_reader=None,
    repo_root=None,
):
    """Capture both exact published parents before compatibility dispatch."""

    from bot_artifact import hash_path
    from checkpoint_schema import live_checkpoint_parent_authority_errors
    from crossover_projection import checkpoint_digest
    from evolution_infra import read_pipeline_checkpoint
    from worker_workflow import WorkerArtifactStore

    subject_errors = _crossover_checkpoint_subject_errors(
        checkpoint,
        parent_a_v=parent_a_v,
        parent_b_v=parent_b_v,
        target_v=target_v,
    )
    if subject_errors:
        _crossover_snapshot_fail(subject_errors[0])
    reader = checkpoint_reader or read_pipeline_checkpoint
    expected_checkpoint_digest = checkpoint_digest(checkpoint)

    def _current_checkpoint_matches(label):
        try:
            current = reader()
        except Exception as exc:
            _crossover_snapshot_fail(
                f"crossover_parent_checkpoint_{label}_read_failed:"
                f"{type(exc).__name__}"
            )
        if checkpoint_digest(current) != expected_checkpoint_digest:
            _crossover_snapshot_fail(
                f"crossover_parent_checkpoint_{label}_drift"
            )

    def _live_authority_errors():
        return live_checkpoint_parent_authority_errors(
            checkpoint,
            repo_root=(repo_root or Path(__file__).resolve().parents[2]),
        )

    _current_checkpoint_matches("before_capture")
    live_errors = _live_authority_errors()
    if live_errors:
        _crossover_snapshot_fail(live_errors[0])

    identities = checkpoint["epoch_binding"]["published_parent_identities"]
    live_dirs = (get_bot_dir(parent_a_v), get_bot_dir(parent_b_v))
    expected_hashes = tuple(
        str(identity.get("tag_artifact_hash") or "") for identity in identities
    )
    try:
        before_hashes = tuple(hash_path(path) for path in live_dirs)
    except Exception as exc:
        _crossover_snapshot_fail(
            "crossover_parent_snapshot_pre_capture_invalid:"
            f"{type(exc).__name__}:{str(exc)[:240]}"
        )
    if before_hashes != expected_hashes:
        _crossover_snapshot_fail("crossover_parent_snapshot_pre_capture_drift")

    store = artifact_store or WorkerArtifactStore(
        RESULTS_DIR / "workflow" / "artifacts"
    )
    try:
        snapshot_hashes = tuple(store.capture(path) for path in live_dirs)
    except Exception as exc:
        _crossover_snapshot_fail(
            "crossover_parent_snapshot_capture_failed:"
            f"{type(exc).__name__}:{str(exc)[:240]}"
        )
    if snapshot_hashes != expected_hashes:
        _crossover_snapshot_fail("crossover_parent_snapshot_capture_hash_mismatch")

    # A mutation after capture must not be hidden by the now-safe immutable
    # snapshots: re-open the complete publication authority and live artifact
    # identities once more before any provider can run.
    live_errors = _live_authority_errors()
    if live_errors:
        _crossover_snapshot_fail(live_errors[0])
    try:
        after_hashes = tuple(hash_path(path) for path in live_dirs)
    except Exception as exc:
        _crossover_snapshot_fail(
            "crossover_parent_snapshot_post_capture_invalid:"
            f"{type(exc).__name__}:{str(exc)[:240]}"
        )
    if after_hashes != expected_hashes:
        _crossover_snapshot_fail("crossover_parent_snapshot_post_capture_drift")
    _current_checkpoint_matches("after_capture")

    parent_entries = []
    for role, version, identity, snapshot_hash in zip(
        ("parent_a", "parent_b"),
        (int(parent_a_v), int(parent_b_v)),
        identities,
        snapshot_hashes,
    ):
        parent_entries.append({
            "role": role,
            "version": version,
            "snapshot_artifact_hash": snapshot_hash,
            "publication_identity": identity,
            "publication_identity_digest": _crossover_snapshot_digest(identity),
        })
    body = {
        "schema_version": 1,
        "kind": "crossover-published-parent-snapshots-v1",
        "target_v": int(target_v),
        "parent_a_v": int(parent_a_v),
        "parent_b_v": int(parent_b_v),
        "workflow_run_id": checkpoint["workflow_run_id"],
        "checkpoint_revision": checkpoint["checkpoint_revision"],
        "checkpoint_stage": checkpoint["stage"],
        "checkpoint_digest": expected_checkpoint_digest,
        "epoch_binding_digest": checkpoint["epoch_binding"]["binding_digest"],
        "parents": parent_entries,
    }
    receipt = {**body, "receipt_digest": _crossover_snapshot_digest(body)}
    return resolve_crossover_parent_snapshots(
        receipt,
        checkpoint=checkpoint,
        parent_a_v=parent_a_v,
        parent_b_v=parent_b_v,
        target_v=target_v,
        artifact_store=store,
    )


def revalidate_crossover_parent_capture(
    snapshot_bundle,
    *,
    checkpoint,
    checkpoint_reader=None,
    repo_root=None,
):
    """Recheck live parent authority immediately before compatibility dispatch."""

    from bot_artifact import hash_path
    from checkpoint_schema import live_checkpoint_parent_authority_errors
    from crossover_projection import checkpoint_digest
    from evolution_infra import read_pipeline_checkpoint

    if not isinstance(snapshot_bundle, dict):
        _crossover_snapshot_fail("crossover_parent_snapshot_bundle_invalid")
    receipt = snapshot_bundle.get("receipt")
    if not isinstance(receipt, dict):
        _crossover_snapshot_fail("crossover_parent_snapshot_receipt_missing")
    reader = checkpoint_reader or read_pipeline_checkpoint
    try:
        current = reader()
    except Exception as exc:
        _crossover_snapshot_fail(
            "crossover_parent_checkpoint_pre_dispatch_read_failed:"
            f"{type(exc).__name__}"
        )
    if checkpoint_digest(current) != checkpoint_digest(checkpoint):
        _crossover_snapshot_fail(
            "crossover_parent_checkpoint_pre_dispatch_drift"
        )
    authority_errors = live_checkpoint_parent_authority_errors(
        checkpoint,
        repo_root=(repo_root or Path(__file__).resolve().parents[2]),
    )
    if authority_errors:
        _crossover_snapshot_fail(authority_errors[0])
    expected_hashes = tuple(
        str(item.get("snapshot_artifact_hash") or "")
        for item in receipt.get("parents") or []
    )
    try:
        live_hashes = (
            hash_path(get_bot_dir(receipt["parent_a_v"])),
            hash_path(get_bot_dir(receipt["parent_b_v"])),
        )
    except Exception as exc:
        _crossover_snapshot_fail(
            "crossover_parent_snapshot_pre_dispatch_live_invalid:"
            f"{type(exc).__name__}:{str(exc)[:240]}"
        )
    if live_hashes != expected_hashes:
        _crossover_snapshot_fail(
            "crossover_parent_snapshot_pre_dispatch_live_drift"
        )


def frozen_crossover_parent_architecture(snapshot_bundle):
    """Derive compatibility/synthesis architecture only from frozen parents."""

    from national_capability_contract import evaluate_national_capabilities
    from runtime_architecture_policy import build_architecture_policy

    if not isinstance(snapshot_bundle, dict):
        _crossover_snapshot_fail("crossover_parent_snapshot_bundle_invalid")
    parent_a_dir = Path(snapshot_bundle.get("frozen_parent_a_dir") or "")
    parent_b_dir = Path(snapshot_bundle.get("frozen_parent_b_dir") or "")
    try:
        parent_a_capabilities = evaluate_national_capabilities(parent_a_dir)
        parent_b_capabilities = evaluate_national_capabilities(parent_b_dir)
        architecture_policy = build_architecture_policy(
            parent_a_dir,
            source_capabilities=parent_a_capabilities,
        )
    except Exception as exc:
        _crossover_snapshot_fail(
            "crossover_parent_frozen_architecture_failed:"
            f"{type(exc).__name__}:{str(exc)[:240]}"
        )

    def _compact(payload):
        return {
            "detector_version": payload.get("detector_version"),
            "checks": {
                item.get("check_id"): bool(item.get("passed"))
                for item in payload.get("checks") or []
                if item.get("check_id")
            },
            "decision_path_risks": {
                key: (payload.get("decision_path_risks") or {}).get(key, [])[:5]
                for key in ("external_io", "history_scans", "large_runtime_tables")
            },
        }

    return {
        "architecture_policy": architecture_policy,
        "capability_context": {
            bot_name(int(snapshot_bundle["receipt"]["parent_a_v"])): _compact(
                parent_a_capabilities
            ),
            bot_name(int(snapshot_bundle["receipt"]["parent_b_v"])): _compact(
                parent_b_capabilities
            ),
        },
    }


def _emit_audit_parse_failure(role, failure_mode, fields=None):
    """Emit a classifiable parse-collapse event for an audit agent.

    Audits are advisory and silently return a safe default when the LLM output
    fails to parse. This helper makes the parse collapse visible (root cause 4
    — parse failure collapsing to an opaque default) by emitting an event_bus
    warn with the classifiable failure_mode (NO_JSON/NO_FENCE/PARSE_ERROR/
    EXCEPTION). Logging only — never raises, never changes control flow.
    """
    try:
        from event_bus import warn
        warn(f"pipeline.{role}_parse_failed",
             f"{role} parse failed (mode={failure_mode}); returning safe default (advisory)",
             failure_mode=failure_mode, **(fields or {}))
    except Exception:
        pass


# ──────────────────────────────────────────────
# P0-1: Post-Master Plan Verification Audit
# ──────────────────────────────────────────────


def _strict_completion_commit_history(limit: int = 5) -> str:
    """Return only exact commits behind published annotated strict tags.

    Ordinary infrastructure commits, untagged candidate work, pre-epoch tags,
    and mutable failure/review text are not direction evidence.  The published
    resolver proves the complete strict ABI; this function independently keeps
    only annotated tag objects and reads exactly their peeled commit bodies.
    """

    from evolution_infra import _git
    from national_runtime_authority import strict_published_bot_names

    versions = sorted({
        version
        for name in strict_published_bot_names()
        if (version := parse_bot_version(name)) is not None
        and version >= FIRST_STRICT_POLICY_VERSION
    })
    rows: list[str] = []
    for version in versions[-max(0, int(limit)):]:
        tag = bot_tag(version)
        if _git("cat-file", "-t", f"refs/tags/{tag}", check=False).strip() != "tag":
            continue
        commit = _git("rev-parse", f"{tag}^{{commit}}", check=False).strip()
        if len(commit) != 40:
            continue
        body = _git("show", "-s", "--format=%B", commit, check=False).strip()
        if not body:
            body = "(completion commit message unavailable)"
        rows.append(f"v{version} [{tag}]\n{body[:1200]}")
    return "\n\n".join(rows)

async def _run_master_plan_audit(master_plan, source_v, ui, next_v=None):
    """Verify Master plan coherence and alignment before Workers execute.

    Returns MasterPlanAuditResult dict.
    Safe default: overall_pass=True (non-blocking).
    """
    safe_default = {
        "plan_coherent": True,
        "contradiction_found": False,
        "contradictions": [],
        "evidence_alignment": "unrelated",
        "direction_novelty": "novel",
        "overall_pass": True,
        "feedback": "",
        "retry_recommended": False,
    }

    try:
        # Completion history is an allowlist, not a generic Git window.  In
        # particular, ``git log <latest-tag> -5`` also admits infrastructure
        # commits and pre-publication attempts between completion identities.
        try:
            recent_commits = _strict_completion_commit_history(limit=5)
        except Exception:
            recent_commits = ""

        # Load direction audit from checkpoint
        direction_audit_text = "No direction audit available"
        try:
            from evolution_infra import read_pipeline_checkpoint
            ckpt = read_pipeline_checkpoint()
            if ckpt and ckpt.get("direction_audit"):
                da = ckpt["direction_audit"]
                if da.get("repetition_detected"):
                    direction_audit_text = json.dumps(da, indent=2, ensure_ascii=False)
        except Exception:
            pass

        target_v = next_v
        if target_v is None:
            target_v = master_plan.get("next_v") or master_plan.get("target_v") or "unknown"
        try:
            from evidence_snapshot import h2h_snapshot_contract_text
            h2h_snapshot_contract = h2h_snapshot_contract_text(
                target_v, source_v=source_v, include_json=True
            )
        except Exception:
            h2h_snapshot_contract = (
                "Stable H2H snapshot unavailable. Do not compare plan citations "
                "against a live head_to_head.json file that may have changed after planning."
            )

        identity_errors = []
        if next_v is not None:
            for key in ("next_v", "target_v", "version"):
                value = master_plan.get(key)
                if value is not None:
                    try:
                        if int(value) != int(next_v):
                            identity_errors.append(f"{key}=v{value} but checkpoint target is v{next_v}")
                    except Exception:
                        identity_errors.append(f"{key}={value!r} but checkpoint target is v{next_v}")
        for key in ("source_v", "parent_version", "branch_from"):
            value = master_plan.get(key)
            if value is not None:
                try:
                    if int(value) != int(source_v):
                        identity_errors.append(f"{key}=v{value} but checkpoint source is v{source_v}")
                except Exception:
                    identity_errors.append(f"{key}={value!r} but checkpoint source is v{source_v}")
        if identity_errors:
            feedback = "; ".join(identity_errors)
            log_system_event(
                "pipeline.master_plan_identity_mismatch", "error",
                f"Master plan identity mismatch for v{target_v}: {feedback}",
                {"source_v": source_v, "next_v": target_v, "errors": identity_errors},
            )
            return {
                "plan_coherent": False,
                "contradiction_found": True,
                "contradictions": identity_errors,
                "evidence_alignment": "misaligned",
                "direction_novelty": "repetitive",
                "overall_pass": False,
                "feedback": feedback,
                "retry_recommended": True,
            }

        log_version = target_v if isinstance(target_v, int) else source_v
        log_file = get_logs_dir(log_version) / "master_plan_audit_io.txt"
        from llm_query import render_llm_prompt

        rendered_prompt = render_llm_prompt(
            "MASTER_PLAN_AUDIT",
            producer=_render_master_plan_audit_provider_prompt,
            renderer_inputs={
                "source_v": int(source_v),
                "next_v": int(target_v),
                "master_plan": master_plan,
                "recent_commits": str(recent_commits),
                "direction_audit": direction_audit_text,
                "h2h_snapshot_contract": h2h_snapshot_contract,
                "recent_directions": _recent_directions_for_audit(),
            },
        )
        output, _, _ = await run_claude_query(
            rendered_prompt, [], ui,
            "MASTER_PLAN_AUDIT", log_file,
            tools=[],
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            data, errors = validate_agent_output("master_plan_auditor", data)
            if errors:
                log.warning("Master plan audit validation: %s", "; ".join(errors[:3]))
                return safe_default
            log.info("Master plan audit: pass=%s, feedback=%s",
                     data.get("overall_pass"), data.get("feedback", "")[:100])
            # Deterministic direction-scoped novelty override: the LLM audit
            # compares against published commit bodies only, so a recycled
            # direction across ABANDONED generations stamps "novel" every
            # time (v174-v187: _bluff_allowed re-proposed 7 gens). The
            # ledger is authoritative here.
            try:
                from recent_directions import recent_symbol_counts

                _selected_symbol = str(
                    ((master_plan.get("proposal_binding") or {}).get(
                        "change_symbol"
                    )) or ""
                )
                _counts = recent_symbol_counts(6, exclude_version=int(next_v))
                _seen = _counts.get(_selected_symbol, 0)
                if _selected_symbol and _seen >= 2:
                    data = dict(data)
                    data["direction_novelty"] = "repetitive"
                    data["overall_pass"] = False
                    data["retry_recommended"] = True
                    data.setdefault("contradictions", [])
                    data["contradictions"] = list(data["contradictions"]) + [
                        f"change_symbol {_selected_symbol} already targeted "
                        f"in {_seen} of the last 6 generation attempts "
                        "(recent directions ledger)"
                    ]
                    log.warning(
                        "Master plan audit override: repetitive direction "
                        "%s (%d/6 recent attempts)",
                        _selected_symbol, _seen,
                    )
            except Exception:
                pass
            return data

    except asyncio.CancelledError:
        raise
    except LLMAvailabilityBlocked:
        # A provider/billing stop must remain attempt-neutral.  Returning the
        # advisory safe default here would incorrectly consume the audit and
        # allow the generation to advance after resume.
        raise
    except Exception as e:
        log.warning("Master plan audit failed: %s. Skipping.", e)
        if is_llm_infra_error(e):
            log_system_event("pipeline.master_plan_audit_infra", "warn",
                             f"Master plan audit LLM crashed (infra): {e}",
                             {"source_v": source_v, "error": str(e)})
            return {**safe_default, "llm_failed": True}

    # Parse collapse: output failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or an
    # exception skipped the parse. Previously this returned safe_default silently.
    _emit_audit_parse_failure("master_plan_audit", locals().get("failure_mode", "EXCEPTION"),
                              {"source_v": source_v})
    return {**safe_default, "parse_failed": True}


# ──────────────────────────────────────────────
# P0-2: Worker CoT Reasoning Consistency Check
# ──────────────────────────────────────────────

def _cot_after_file_state(path):
    """Return a safe text-or-bytes state for Worker CoT evidence."""
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError:
        return False, "missing", b""
    if not stat.S_ISREG(metadata.st_mode):
        return False, "invalid", b""
    try:
        data = read_regular_file_bytes(path.parent, path, metadata)
    except OSError:
        return False, "invalid", b""
    if is_binary_artifact_path(path):
        return True, "binary", data
    try:
        return True, "text", data.decode("utf-8")
    except UnicodeDecodeError:
        return True, "binary", data


def _cot_binary_metadata(label, present, content):
    if not present:
        return f"{label}: missing"
    data = content if isinstance(content, bytes) else str(content).encode("utf-8")
    return (
        f"{label}: {len(data)} bytes, sha256={hashlib.sha256(data).hexdigest()}"
    )

async def _run_worker_cot_check(
    task,
    worker_idx,
    next_v,
    source_v,
    next_dir,
    worker_snapshots,
    ui,
    *,
    worker_output_evidence=None,
):
    """Check Worker output consistency: claimed changes vs actual diff.

    Returns WorkerCoTCheckResult dict.
    Safe default: cot_consistent=True (non-blocking).
    """
    w_id = task.get("worker_id", worker_idx + 1)
    safe_default = {
        "worker_id": w_id,
        "cot_consistent": True,
        "discrepancies": [],
        "logical_contradictions": [],
        "boundary_violations": [],
        "focus_areas": [],
    }

    try:
        if worker_output_evidence is None:
            return safe_default
        fenced_output = _open_fenced_worker_output(
            worker_output_evidence,
            task=task,
            worker_id=w_id,
            next_v=next_v,
            source_v=source_v,
        )
        if fenced_output["output_bytes"] == 0:
            return safe_default

        # Compute diff for this worker's target files using snapshots
        diff_parts = []
        diff_metadata = []
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if not rel:
                continue
            snapshot_key = (worker_idx, rel)
            before_present = snapshot_key in worker_snapshots
            before = worker_snapshots.get(snapshot_key, "")
            after_path = next_dir / rel
            after_present, after_kind, after = _cot_after_file_state(after_path)
            binary_evidence = isinstance(before, bytes) or after_kind in {
                "binary", "invalid"
            }
            if binary_evidence:
                before_meta = _cot_binary_metadata(
                    "before", before_present, before
                )
                after_meta = _cot_binary_metadata(
                    "after", after_present, after
                )
                changed = not before_present or not after_present or before != after
                diff_metadata.append(
                    f"- {rel}: binary artifact; {before_meta}; {after_meta}; "
                    f"changed={str(changed).lower()}"
                )
                if changed:
                    diff_parts.append(
                        f"--- before/{rel} (binary metadata)\n"
                        f"+++ after/{rel} (binary metadata)\n"
                        f"-{before_meta}\n+{after_meta}\n"
                    )
                continue

            before_text = str(before) if before_present else ""
            after_text = str(after) if after_present else ""
            before_lines = len(before_text.splitlines())
            after_lines = len(after_text.splitlines())
            diff_metadata.append(
                f"- {rel}: pre-worker snapshot {before_lines} lines; "
                f"post-worker file {after_lines} lines; delta {after_lines - before_lines:+d}"
            )
            if not before_present or not after_present or before_text != after_text:
                diff = difflib.unified_diff(
                    before_text.splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
                    fromfile=f"before/{rel}", tofile=f"after/{rel}",
                    n=3,
                )
                diff_text = "".join(diff)
                if diff_text:
                    diff_parts.append(diff_text)

        if not diff_parts:
            return safe_default

        code_diff = "\n".join(diff_parts)[-6000:]

        log_file = get_logs_dir(next_v) / f"worker_{w_id}_cot_audit_io.txt"
        from llm_query import render_llm_prompt

        cot_role = f"WORKER_COT_CHECK_{w_id}"
        rendered_prompt = render_llm_prompt(
            cot_role,
            producer=_render_worker_cot_provider_prompt,
            renderer_inputs={
                "task": task,
                "worker_role": task.get("role", "Worker"),
                "worker_task": task.get(
                    "worker_prompt", task.get("instruction", "")
                ),
                "worker_output_evidence": fenced_output,
                "code_diff": code_diff,
                "diff_metadata": (
                    "\n".join(diff_metadata) or "- no target file metadata"
                ),
            },
        )
        output, _, _ = await run_claude_query(
            rendered_prompt, [], ui,
            cot_role, log_file,
            tools=[],
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            data.setdefault("worker_id", w_id)
            data, errors = validate_agent_output("worker_cot_checker", data)
            if errors:
                log.warning("Worker CoT check validation: %s", "; ".join(errors[:3]))
                return safe_default
            consistent = data.get("cot_consistent", True)
            log.info("Worker %s CoT check: consistent=%s", w_id, consistent)
            if not consistent:
                log_system_event("pipeline.worker_cot_inconsistency", "warn",
                                 f"Worker {w_id} CoT inconsistency: {data.get('discrepancies', [])[:2]}",
                                 {"worker_id": w_id, "discrepancies": data.get("discrepancies", [])[:3]})
            return data

    except WorkerCoTEvidenceError:
        raise
    except LLMAvailabilityBlocked:
        raise
    except Exception as e:
        log.warning("Worker CoT check failed: %s. Skipping.", e)
        if is_llm_infra_error(e):
            log_system_event("pipeline.worker_cot_check_infra", "warn",
                             f"Worker {w_id} CoT check LLM crashed (infra): {e}",
                             {"worker_id": w_id, "next_v": next_v, "error": str(e)})
            return {**safe_default, "llm_failed": True}

    # Parse collapse: output failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or an
    # exception skipped the parse. Previously this returned safe_default silently.
    _emit_audit_parse_failure("worker_cot_check", locals().get("failure_mode", "EXCEPTION"),
                              {"worker_id": w_id, "next_v": next_v})
    return {**safe_default, "parse_failed": True}


# ──────────────────────────────────────────────
# P0-3: LLM-Generated Dynamic Decision Tests
# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
# P0-4: Precommit Eval Semantic Interpretation
# ──────────────────────────────────────────────


async def _run_degeneration_diagnosis(source_v, recent_commits, strategy_changes, rating_curve, ui):
    """Diagnose root cause of continuous rating degeneration.

    Returns DegenerationDiagnosis dict.
    Safe default: is_degenerating=False (non-blocking).
    """
    safe_default = {
        "is_degenerating": False,
        "root_causes": [],
        "commit_evidence": [],
        "strategy_drift_evidence": [],
        "recommendation": "continue",
        "urgent_intervention": False,
    }

    try:
        log_file = get_logs_dir(source_v) / "degeneration_diagnosis_io.txt"
        from llm_query import render_llm_prompt

        rendered_prompt = render_llm_prompt(
            "DEGENERATION_DIAGNOSIS",
            producer=_render_degeneration_provider_prompt,
            renderer_inputs={
                "source_v": int(source_v),
                "recent_commits": str(recent_commits),
                "strategy_changes": str(strategy_changes),
                "rating_curve": str(rating_curve),
            },
        )
        output, _, _ = await run_claude_query(
            rendered_prompt, [], ui,
            "DEGENERATION_DIAGNOSIS", log_file,
            tools=[],
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            data, errors = validate_agent_output("degeneration_diagnosis", data)
            if errors:
                log.warning("Degeneration diagnosis validation: %s", "; ".join(errors[:3]))
                return safe_default
            return data

    except LLMAvailabilityBlocked:
        # Provider availability is global control flow, not an advisory
        # no-degeneration judgement.  The prepare stage must remain byte- and
        # attempt-neutral until the exact pause receipt is reconciled.
        raise
    except Exception as e:
        log.warning("Degeneration diagnosis failed: %s. Skipping.", e)
        if is_llm_infra_error(e):
            log_system_event("pipeline.degeneration_diagnosis_infra", "warn",
                             f"Degeneration diagnosis v{source_v} LLM crashed (infra): {e}",
                             {"source_v": source_v, "error": str(e)})
            return {**safe_default, "llm_failed": True}

    # Parse collapse: output failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or an
    # exception skipped the parse. Previously this returned safe_default silently.
    _emit_audit_parse_failure("degeneration_diagnosis", locals().get("failure_mode", "EXCEPTION"),
                              {"source_v": source_v})
    return {**safe_default, "parse_failed": True}


# ──────────────────────────────────────────────
# P1-3: Crossover Parent Compatibility Audit
# ──────────────────────────────────────────────

async def _run_crossover_compatibility_audit(
    parent_a_v,
    parent_b_v,
    ui,
    *,
    target_v=None,
    authoritative_checkpoint=None,
):
    """Audit compatibility of two crossover parent bots.

    Returns CrossoverCompatibilityResult dict.
    Safe default: compatible=True (non-blocking).
    """
    safe_default = {
        "compatible": True,
        "compatibility_score": 7,
        "conflict_areas": [],
        "suggested_merge_approach": "",
        "files_to_take_from_a": [],
        "files_to_take_from_b": [],
    }

    if target_v is None:
        _crossover_snapshot_fail("crossover_parent_snapshot_target_missing")
    if authoritative_checkpoint is None:
        from evolution_infra import read_pipeline_checkpoint

        authoritative_checkpoint = read_pipeline_checkpoint()
    snapshot_bundle = capture_crossover_parent_snapshots(
        parent_a_v,
        parent_b_v,
        target_v,
        checkpoint=authoritative_checkpoint,
    )
    frozen_architecture = frozen_crossover_parent_architecture(snapshot_bundle)
    snapshot_receipt = snapshot_bundle["receipt"]
    system_result = {
        "parent_snapshot_receipt": snapshot_receipt,
        "frozen_architecture_policy": frozen_architecture["architecture_policy"],
        "frozen_capability_context": frozen_architecture["capability_context"],
    }

    def _bound(result):
        return {**result, **system_result}

    try:
        # policy.py is the sole candidate-owned source artifact.  Runtime and
        # precompute bytes are system-owned and never crossover inputs.
        def _frozen_policy(root):
            policy = Path(root) / "policy.py"
            metadata = policy.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                _crossover_snapshot_fail(
                    "crossover_parent_snapshot_policy_not_regular"
                )
            return read_regular_file_bytes(
                Path(root), policy, metadata
            ).decode("utf-8", "strict")[:4000]

        parent_a_code = {
            "policy.py": _frozen_policy(
                snapshot_bundle["frozen_parent_a_dir"]
            )
        }
        parent_b_code = {
            "policy.py": _frozen_policy(
                snapshot_bundle["frozen_parent_b_dir"]
            )
        }

        rating_a = "unknown"
        rating_b = "unknown"
        h2h_context = "Stable H2H snapshot unavailable. Treat matchup strength as unknown."
        if target_v is not None:
            try:
                from evidence_snapshot import load_generation_evaluation_snapshot
                from evolution_infra import pair_key

                frozen = load_generation_evaluation_snapshot(target_v)
                if not frozen.get("available"):
                    raise RuntimeError(
                        f"generation snapshot unavailable: {frozen.get('reason')}"
                    )
                ratings = frozen.get("ratings") or {}
                ra = ratings.get(bot_name(parent_a_v)) or {}
                rb = ratings.get(bot_name(parent_b_v)) or {}
                if isinstance(ra, dict) and ra:
                    rating_a = f"{float(ra.get('r', 1500)):.1f} ± {float(ra.get('rd', 350)):.1f}"
                if isinstance(rb, dict) and rb:
                    rating_b = f"{float(rb.get('r', 1500)):.1f} ± {float(rb.get('rd', 350)):.1f}"
                h2h = frozen.get("h2h") or {}
                key = pair_key(bot_name(parent_a_v), bot_name(parent_b_v))
                row = h2h.get(key) if isinstance(h2h, dict) else None
                if isinstance(row, dict):
                    h2h_context = (
                        f"{key}: games={int(row.get('games', 0) or 0)}, "
                        f"a_wins={int(row.get('a_wins', 0) or 0)}, "
                        f"b_wins={int(row.get('b_wins', 0) or 0)}, "
                        f"draws={int(row.get('draws', 0) or 0)}"
                    )
                else:
                    h2h_context = f"Stable snapshot has no row for {key}; matchup is sparse/unknown."
            except Exception as exc:
                h2h_context = f"Stable H2H snapshot read failed: {type(exc).__name__}: {str(exc)[:160]}"

        log_file = get_logs_dir(parent_a_v) / f"crossover_compat_{parent_a_v}x{parent_b_v}_io.txt"
        from llm_query import render_llm_prompt

        compat_role = f"CROSSOVER_COMPAT_{parent_a_v}x{parent_b_v}"
        # Detect corruption or replacement of either content-addressed tree
        # after prompt material was read but before provider dispatch.
        resolve_crossover_parent_snapshots(
            snapshot_receipt,
            checkpoint=authoritative_checkpoint,
            parent_a_v=parent_a_v,
            parent_b_v=parent_b_v,
            target_v=target_v,
        )
        revalidate_crossover_parent_capture(
            snapshot_bundle,
            checkpoint=authoritative_checkpoint,
        )
        rendered_prompt = render_llm_prompt(
            compat_role,
            producer=_render_crossover_compat_provider_prompt,
            renderer_inputs={
                "parent_a_v": int(parent_a_v),
                "parent_b_v": int(parent_b_v),
                "parent_a_code": parent_a_code,
                "parent_b_code": parent_b_code,
                "parent_a_rating": str(rating_a),
                "parent_b_rating": str(rating_b),
                "h2h_context": h2h_context,
                "architecture_context": {
                    "architecture_policy": frozen_architecture[
                        "architecture_policy"
                    ],
                    "parent_capabilities": frozen_architecture[
                        "capability_context"
                    ],
                    "parent_snapshot_receipt_digest": snapshot_receipt[
                        "receipt_digest"
                    ],
                },
                "parent_snapshot_receipt": snapshot_receipt,
            },
        )
        output, _, _ = await run_claude_query(
            rendered_prompt, [], ui,
            compat_role, log_file,
            tools=[],
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            data, errors = validate_agent_output("crossover_compatibility", data)
            if errors:
                log.warning("Crossover compatibility validation: %s", "; ".join(errors[:3]))
                return _bound(safe_default)
            return _bound(data)

    except LLMAvailabilityBlocked:
        raise
    except CrossoverParentSnapshotError:
        raise
    except Exception as e:
        log.warning("Crossover compatibility audit failed: %s. Skipping.", e)
        if is_llm_infra_error(e):
            log_system_event("pipeline.crossover_compat_infra", "warn",
                             f"Crossover compat v{parent_a_v}xv{parent_b_v} LLM crashed (infra): {e}",
                             {"parent_a_v": parent_a_v, "parent_b_v": parent_b_v, "error": str(e)})
            return _bound({**safe_default, "llm_failed": True})

    # Parse collapse: output failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or an
    # exception skipped the parse. Previously this returned safe_default silently.
    _emit_audit_parse_failure("crossover_compatibility", locals().get("failure_mode", "EXCEPTION"),
                              {"parent_a_v": parent_a_v, "parent_b_v": parent_b_v})
    return _bound({**safe_default, "parse_failed": True})
