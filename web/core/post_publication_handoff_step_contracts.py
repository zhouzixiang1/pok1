"""Step plan / output / receipt / row validators for post_publication_handoff.

Extracted as a cohesive business cluster from post_publication_handoff.py.
This companion owns the per-step contract validators for the eight handoff
steps (stability_observation, reap_signal, priority_eval, archive_rotation,
log_cleanup, pool_reap, cycle_annotation, housekeeping): the plan-output
contract checks, the reprove (operational + external) drivers, and the
receipt / row shape validators.

CRITICAL (wave-3 lesson): every intra-companion call to a moved symbol AND
every reference to a stay-behind main-module helper is routed through
``_pph.<name>(...)`` so that ``monkeypatch.setattr(post_publication_handoff,
"<name>", ...)`` fired by tests keeps propagating after the move. A function
calling ITSELF recursively may stay bare; everything else goes through ``_pph.``.

The main module re-exports every symbol below via a bottom-of-file
``from post_publication_handoff_step_contracts import (...)`` block, so existing
``from post_publication_handoff import <name>`` callers and test monkeypatches
applied to ``post_publication_handoff`` keep resolving unchanged.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any

from bot_artifact import canonical_digest
from bot_namespace import FIRST_STRICT_POLICY_VERSION, bot_name
from publication_reproof import (
    FieldSpec,
    ReproofContract,
    register_contract,
    run_reproof,
)

import post_publication_handoff as _pph  # for intra-companion + stay-behind refs


# ---------------------------------------------------------------------------
# Frozen-vs-live reproof engine seam (2026-10-22 migration)
# ---------------------------------------------------------------------------
# Every frozen-vs-live drift judgment in this module grades through the pure
# engine in ``publication_reproof.py`` (engine catalog sites:
# ``handoff.stability_observation_step`` — the reset witness, ``handoff
# .reap_signal`` / ``handoff.priority_eval`` — the reissue digests,
# ``archivist.pool_reap_ring`` — the five-field pool ring, ``handoff
# .housekeeping`` — the worktree ring, ``handoff.housekeeping.reap_proofs``
# — the per-bot reap proofs).  The probes reuse the main module's GitProbe
# adapter so the operator/test monkeypatch surfaces (the handoff module's
# ancestry predicate, the archivist's ``_publication_head_advance_allowed``)
# keep governing every ancestry judgment.

# The housekeeping output contract's observed-head leg: the receipt records
# the head observed at verification time, which legitimately advanced past
# the publication commit (eleventh exact-pin, v579).
_OUTPUT_HEAD_PROBE = _pph._SeamGitProbe(
    lambda frozen_oid, live_oid: _pph._publication_commit_is_ancestor_of(
        frozen_oid, live_oid
    )
)

register_contract(ReproofContract(
    site_id="handoff.housekeeping_output_head",
    fields=(
        FieldSpec(
            "head_oid", "git_ancestry", "frozen_observation",
            "handoff_step_output_contract_invalid:housekeeping:identity",
            {"tip_kind": "literal_oid", "hex": 40, "equal_passes": True},
        ),
    ),
    status="wired",
))
# Explicit-test-mode (local-only) reap proofs carry a remote_proof mapping of
# ``{"required": False}`` only; the engine catalog's per-bot contract
# (handoff.housekeeping.reap_proofs) declares the required-remote field set,
# which a local-only mapping can never carry.  Those env-gated shapes keep
# the historical whole-record equality grade through this exact site.
register_contract(ReproofContract(
    site_id="handoff.housekeeping.reap_proofs_exact",
    fields=(
        FieldSpec(
            "reap_proofs", "exact", "container",
            "handoff_housekeeping_reap_reproof_mismatch", {},
        ),
    ),
    status="wired",
))


_STEP_RECEIPT_KEYS = frozenset({
    "schema_version", "step", "publication_id", "completed_at",
    "plan_digest", "output", "receipt_digest",
})

def _contract_error(step: str, subject: str, detail: str) -> str:
    return f"handoff_step_{subject}_contract_invalid:{step}:{detail}"



def _exact_object(
    value: Any,
    keys: set[str] | frozenset[str],
) -> bool:
    return isinstance(value, dict) and set(value) == set(keys)



def _step_plan_contract_errors(
    name: str,
    plan: Any,
    identity: dict[str, Any],
) -> list[str]:
    """Validate the exact producer contract before any effect may execute."""

    if not isinstance(plan, dict):
        return [_pph._contract_error(name, "plan", "not_object")]
    errors: list[str] = []
    publication_id = identity.get("publication_id")
    version = identity.get("version")

    def reject(detail: str) -> None:
        errors.append(_pph._contract_error(name, "plan", detail))

    if name == "stability_observation":
        if not _pph._exact_object(plan, {
            "schema_version", "kind", "publication_id",
            "publishing_checkpoint_digest",
            "strength_evidence_identity_digest",
        }):
            reject("fields")
        if plan.get("schema_version") != 1 or plan.get(
            "kind"
        ) != "stability-observation-plan":
            reject("identity")
        if plan.get("publication_id") != publication_id:
            reject("publication")
        if plan.get("publishing_checkpoint_digest") != identity.get(
            "publishing_checkpoint_digest"
        ):
            reject("checkpoint")
        if not _pph._is_hex(plan.get("strength_evidence_identity_digest"), 64):
            reject("evidence_digest")
    elif name == "reap_signal":
        if not _pph._exact_object(plan, {
            "schema_version", "kind", "publication_id", "signal_text",
            "signal_sha256",
        }):
            reject("fields")
        signal = plan.get("signal_text")
        if (
            plan.get("schema_version") != 1
            or plan.get("kind") != "rating-daemon-refresh-plan"
            or plan.get("publication_id") != publication_id
        ):
            reject("identity")
        if not isinstance(signal, str) or not signal or len(signal) > 128:
            reject("signal_text")
        elif hashlib.sha256(signal.encode("utf-8")).hexdigest() != plan.get(
            "signal_sha256"
        ):
            reject("signal_digest")
    elif name == "priority_eval":
        payload = plan.get("payload")
        if not _pph._exact_object(plan, {"schema_version", "kind", "payload"}):
            reject("fields")
        if (
            plan.get("schema_version") != 1
            or plan.get("kind") != "priority-evaluation-plan"
        ):
            reject("identity")
        if not _pph._exact_object(
            payload, {"bot", "min_games", "since", "publication_id"}
        ):
            reject("payload_fields")
        elif (
            payload.get("bot") != bot_name(int(version or -1))
            or payload.get("min_games") != 500
            or not _pph._finite_time(payload.get("since"))
            or payload.get("publication_id") != publication_id
        ):
            reject("payload_identity")
    elif name == "archive_rotation":
        try:
            from evolution_infra import _validate_archive_rotation_plan_shape

            _validate_archive_rotation_plan_shape(
                plan,
                version=int(version or -1),
                publication_id=publication_id,
            )
        except Exception as exc:
            reject(f"identity:{type(exc).__name__}:{str(exc)[:160]}")
    elif name == "log_cleanup":
        if not _pph._exact_object(plan, {
            "schema_version", "kind", "handoff_version",
            "first_strict_version", "keep_generations", "cutoff_version",
            "archives", "publication_id",
        }):
            reject("fields")
        keep = plan.get("keep_generations")
        cutoff = plan.get("cutoff_version")
        archives = plan.get("archives")
        if (
            plan.get("schema_version") != 1
            or plan.get("kind") != "strict-log-cleanup-plan"
            or plan.get("handoff_version") != version
            or plan.get("first_strict_version")
            != FIRST_STRICT_POLICY_VERSION
            or keep != 5
            or type(cutoff) is not int
            or cutoff != int(version or -1) - 5
            or plan.get("publication_id") != publication_id
            or not isinstance(archives, list)
        ):
            reject("identity")
        if isinstance(archives, list):
            seen: set[int] = set()
            for item in archives:
                if not _pph._exact_object(item, {
                    "schema_version", "kind", "version",
                    "source_relative_path", "entries", "tree_digest",
                    "archive_relative_path", "manifest_relative_path",
                    "quarantine_relative_path",
                }):
                    reject("archive_fields")
                    continue
                item_version = item.get("version")
                tree_payload = {
                    key: item.get(key)
                    for key in (
                        "schema_version", "kind", "version",
                        "source_relative_path", "entries",
                    )
                }
                if (
                    type(item_version) is not int
                    or item_version < FIRST_STRICT_POLICY_VERSION
                    or type(cutoff) is not int
                    or item_version > cutoff
                    or item_version in seen
                    or item.get("schema_version") != 1
                    or item.get("kind") != "strict-generation-log-tree"
                    or item.get("source_relative_path")
                    != f"v{item_version}/logs"
                    or not isinstance(item.get("entries"), list)
                    or item.get("tree_digest")
                    != canonical_digest(tree_payload)
                ):
                    reject("archive_identity")
                seen.add(item_version) if type(item_version) is int else None
    elif name == "pool_reap":
        # Reuse the pure schema-2 verifier so journal discovery, claim, and the
        # executor all prove the same frozen conservative-Glicko target
        # sequence without reopening mutable rating files.
        try:
            from tool_commit import _validate_pool_reap_plan

            _validate_pool_reap_plan(plan, {"identity": identity})
        except Exception as exc:
            reject(f"selection:{type(exc).__name__}:{str(exc)[:160]}")
    elif name == "cycle_annotation":
        if not _pph._exact_object(plan, {
            "schema_version", "kind", "publication_id",
            "archive_pre_annotation_digest",
        }):
            reject("fields")
        if (
            plan.get("schema_version") != 1
            or plan.get("kind") != "cycle-archivist-annotation-plan"
            or plan.get("publication_id") != publication_id
            or not _pph._is_hex(plan.get("archive_pre_annotation_digest"), 64)
        ):
            reject("identity")
    elif name == "housekeeping":
        dependencies = plan.get("dependency_receipts")
        if not _pph._exact_object(plan, {
            "schema_version", "kind", "expected_head_oid",
            "expected_dirty_paths", "tracked_housekeeping_commit_allowed",
            "dependency_receipts",
        }):
            reject("fields")
        if (
            plan.get("schema_version") != 1
            or plan.get("kind")
            != "post-publication-worktree-verification-plan"
            or plan.get("expected_head_oid") != identity.get("commit_oid")
            or plan.get("expected_dirty_paths") != []
            or plan.get("tracked_housekeeping_commit_allowed") is not False
            or not _pph._exact_object(dependencies, {
                "archive_rotation", "log_cleanup", "pool_reap",
                "cycle_annotation",
            })
            or any(not _pph._is_hex(value, 64) for value in (
                dependencies.values() if isinstance(dependencies, dict) else []
            ))
        ):
            reject("identity")
    return list(dict.fromkeys(errors))



def _step_output_contract_errors(
    name: str,
    output: Any,
    plan: dict[str, Any],
    plan_digest: str,
    identity: dict[str, Any],
) -> list[str]:
    if not isinstance(output, dict):
        return [_pph._contract_error(name, "output", "not_object")]
    errors: list[str] = []
    publication_id = identity.get("publication_id")
    version = identity.get("version")

    def reject(detail: str) -> None:
        errors.append(_pph._contract_error(name, "output", detail))

    if output.get("plan_digest") != plan_digest:
        reject("plan_binding")
    if name == "stability_observation":
        if not _pph._exact_object(output, {
            "plan_digest", "publication_id", "continuity_id", "count",
            "target", "complete",
        }):
            reject("fields")
        if (
            output.get("publication_id") != publication_id
            or not _pph._is_hex(output.get("continuity_id"), 32)
            or type(output.get("count")) is not int
            or output.get("count") < 0
            or output.get("count") > 10
            or output.get("target") != 10
            or type(output.get("complete")) is not bool
            or (output.get("complete") is True and output.get("count") < 10)
        ):
            reject("identity")
    elif name == "reap_signal":
        if not _pph._exact_object(
            output, {"plan_digest", "publication_id", "signal_sha256"}
        ):
            reject("fields")
        if (
            output.get("publication_id") != publication_id
            or output.get("signal_sha256") != plan.get("signal_sha256")
        ):
            reject("identity")
    elif name == "priority_eval":
        if not _pph._exact_object(output, {
            "plan_digest", "bot", "min_games", "publication_id",
            "payload_sha256",
        }):
            reject("fields")
        payload = plan.get("payload")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
        if (
            output.get("bot") != bot_name(int(version or -1))
            or output.get("min_games") != 500
            or output.get("publication_id") != publication_id
            or output.get("payload_sha256")
            != hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        ):
            reject("identity")
    elif name == "archive_rotation":
        rotations = output.get("rotations")
        if not _pph._exact_object(output, {
            "plan_digest", "version", "rotations", "rotation_set_digest",
        }):
            reject("fields")
        if (
            output.get("version") != version
            or not isinstance(rotations, list)
            or output.get("rotation_set_digest") != canonical_digest(rotations)
        ):
            reject("identity")
        if isinstance(rotations, list):
            for item in rotations:
                if not _pph._exact_object(item, {
                    "source", "rotation_id", "plan_digest",
                    "archive_sha256", "start_offset", "end_offset",
                    "source_preserved_append_only",
                }) or any(
                    not _pph._is_hex(item.get(key), 64)
                    for key in (
                        "rotation_id", "plan_digest", "archive_sha256",
                    )
                ) or (
                    type(item.get("start_offset")) is not int
                    or type(item.get("end_offset")) is not int
                    or item.get("start_offset") < 0
                    or item.get("end_offset") <= item.get("start_offset")
                    or item.get("source_preserved_append_only") is not True
                ):
                    reject("rotation_receipt")
            try:
                from evolution_infra import expected_archive_rotation_receipts

                expected_rotations = expected_archive_rotation_receipts(
                    plan,
                    version=int(version or -1),
                    publication_id=identity.get("publication_id"),
                )
            except Exception as exc:
                reject(
                    f"rotation_plan:{type(exc).__name__}:{str(exc)[:160]}"
                )
            else:
                if rotations != expected_rotations:
                    reject("rotation_set_mismatch")
    elif name == "log_cleanup":
        archives = output.get("archives")
        if not _pph._exact_object(output, {
            "plan_digest", "version", "archives", "archive_set_digest",
        }):
            reject("fields")
        if (
            output.get("version") != version
            or not isinstance(archives, list)
            or output.get("archive_set_digest") != canonical_digest(archives)
        ):
            reject("identity")
        if isinstance(archives, list) and len(archives) != len(
            plan.get("archives") or []
        ):
            reject("archive_count")
        if isinstance(archives, list):
            planned = {
                item.get("version"): item
                for item in (plan.get("archives") or [])
                if isinstance(item, dict)
            }
            for receipt in archives:
                if not _pph._exact_object(receipt, {
                    "version", "tree_digest", "archive_relative_path",
                    "archive_sha256", "manifest_relative_path",
                    "manifest_digest", "effect_mode",
                    "live_source_relative_path", "live_log_tree_preserved",
                    "quarantine_log_tree_touched",
                    "generation_siblings_preserved",
                }):
                    reject("archive_receipt_fields")
                    continue
                subject = planned.get(receipt.get("version")) or {}
                if (
                    receipt.get("tree_digest") != subject.get("tree_digest")
                    or receipt.get("archive_relative_path")
                    != subject.get("archive_relative_path")
                    or receipt.get("manifest_relative_path")
                    != subject.get("manifest_relative_path")
                    or receipt.get("live_source_relative_path")
                    != subject.get("source_relative_path")
                    or not _pph._is_hex(receipt.get("archive_sha256"), 64)
                    or not _pph._is_hex(receipt.get("manifest_digest"), 64)
                    or receipt.get("effect_mode")
                    != "nondestructive-immutable-archive"
                    or receipt.get("live_log_tree_preserved") is not True
                    or receipt.get("quarantine_log_tree_touched") is not False
                    or receipt.get("generation_siblings_preserved") is not True
                ):
                    reject("archive_receipt_identity")
    elif name == "pool_reap":
        if not _pph._exact_object(output, {
            "plan_digest", "removed_bots", "required_reaps", "reap_proofs",
            "reap_proof_set_digest",
        }):
            reject("fields")
        proofs = output.get("reap_proofs")
        removed = output.get("removed_bots")
        target_names = [
            row.get("candidate")
            for row in (plan.get("targets") or [])
            if isinstance(row, dict)
        ]
        if (
            not isinstance(removed, list)
            or not isinstance(proofs, list)
            or output.get("required_reaps") != plan.get("required_reaps")
            or removed != sorted(target_names)
            or len(proofs) != len(target_names)
            or sorted(
                proof.get("bot")
                for proof in proofs
                if isinstance(proof, dict)
            ) != sorted(target_names)
            or output.get("reap_proof_set_digest") != canonical_digest(proofs)
        ):
            reject("identity")
    elif name == "cycle_annotation":
        if not _pph._exact_object(output, {
            "plan_digest", "annotation_digest", "archive_semantic_digest",
        }):
            reject("fields")
        if not _pph._is_hex(output.get("annotation_digest"), 64) or not _pph._is_hex(
            output.get("archive_semantic_digest"), 64
        ):
            reject("identity")
    elif name == "housekeeping":
        if not _pph._exact_object(output, {
            "plan_digest", "head_oid", "worktree_status_digest",
            "tracked_housekeeping_commit", "archive_rotation_revalidated",
            "strict_log_archives_revalidated", "reap_proofs",
            "reap_proof_set_digest",
        }):
            reject("fields")
        proofs = output.get("reap_proofs")
        # HEAD-advance rule (2026-10-18, eleventh exact-pin of the v566
        # convergence family): the receipt records the head actually observed
        # at verification time, which legitimately advanced past the
        # publication commit when a post-publication bug-fix deploy landed
        # (this campaign itself).  The executor already applies the ancestor
        # rule (_verify_post_publication_worktree); the head leg grades
        # through the reproof engine (equal-or-ancestor, probe failure folds
        # closed) — exact equality stays the common no-deploy case and any
        # non-ancestral divergence still fails closed.
        head_ok = run_reproof(
            "handoff.housekeeping_output_head",
            frozen={"head_oid": identity.get("commit_oid")},
            live={"head_oid": output.get("head_oid")},
            probe=_OUTPUT_HEAD_PROBE,
        ).ok
        if (
            not head_ok
            or not _pph._is_hex(output.get("worktree_status_digest"), 64)
            or output.get("tracked_housekeeping_commit") is not False
            or output.get("archive_rotation_revalidated") is not True
            or output.get("strict_log_archives_revalidated") is not True
            or not isinstance(proofs, list)
            or output.get("reap_proof_set_digest") != canonical_digest(proofs)
        ):
            reject("identity")
    return list(dict.fromkeys(errors))



def _reprove_operational_steps(record: dict[str, Any]) -> dict[str, bool]:
    """Re-open stability state and idempotently reissue daemon capabilities.

    Receipt digests prove journal integrity, not that an external effect still
    exists.  A retry and finalization therefore re-prove the durable stability
    row and re-publish the exact content-bound signal/priority payload.  The
    daemon may consume either file immediately after the sidecar lock releases;
    that consumption is the intended effect.

    The stability row is process-scoped BY DESIGN (twelfth convergence defect,
    v566): a runtime restart or documented continuity break resets it to zero,
    so the frozen receipt can never re-prove byte-exactly across a reset.  The
    row and its reset witness grade through the reproof engine site
    ``handoff.stability_observation_step`` (witness_reset): an equal row is a
    match, a row proven reset by a qualifying reset-history entry is
    drift_allowed, anything else is the hard reproof mismatch.  The sidecar
    reissue (locked write + re-read) stays in this adapter; the engine only
    grades the reissued digests.
    """

    steps = record.get("steps") or {}
    identity = record.get("identity") or {}
    result = {
        "stability_observation": False,
        "reap_signal": False,
        "priority_eval": False,
    }
    stability = steps.get("stability_observation") or {}
    if stability.get("status") == "completed":
        from stability_observation import stability_observation_projection

        projection = stability_observation_projection()
        receipt = stability.get("receipt") or {}
        output = receipt.get("output") or {}
        row_fields = ("continuity_id", "count", "target", "complete")
        reproof = run_reproof(
            "handoff.stability_observation_step",
            frozen={"row": {key: output.get(key) for key in row_fields}},
            live={
                "row": {
                    "continuity_id": projection.get("continuity_id"),
                    "count": projection.get("count"),
                    "target": projection.get("target"),
                    "complete": projection.get("complete"),
                }
            },
            context={
                # The witness history is the projection's reset record:
                # stability_observation renders it whenever the state row is
                # internally consistent, including immediately after a
                # documented reset (the pre-reset row binding lives at ENTRY
                # level: at / previous_count / previous_last_version).
                "witness_history": projection.get("reset_history"),
                "receipt": receipt,
                "identity": identity,
                "output": output,
            },
        )
        row_verdict = next(
            (row for row in reproof.results if row.field == "row"), None
        )
        if row_verdict is None or row_verdict.outcome == "mismatch":
            raise _pph.PostPublicationHandoffError(
                "handoff_stability_observation_reproof_mismatch"
            )
        if row_verdict.outcome == "match":
            publication_id = identity.get("publication_id")
            observations = projection.get("observations")
            if not isinstance(observations, list) or not any(
                isinstance(row, dict)
                and row.get("publication_id") == publication_id
                for row in observations
            ):
                raise _pph.PostPublicationHandoffError(
                    "handoff_stability_publication_row_missing"
                )
        result["stability_observation"] = True

    from evolution_infra import (
        RESULTS_DIR,
        _atomic_publish_state_text,
        _locked_state_sidecar,
        _read_regular_state_text,
    )

    signal = steps.get("reap_signal") or {}
    if signal.get("status") == "completed":
        plan = signal.get("plan") or {}
        output = (signal.get("receipt") or {}).get("output") or {}
        raw = str(plan.get("signal_text") or "")
        path = Path(RESULTS_DIR) / ".reap_signal"
        with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
            _atomic_publish_state_text(path, raw)
            reopened = _read_regular_state_text(path, allow_missing=False)
        digest = hashlib.sha256(reopened.encode("utf-8")).hexdigest()
        reissue_issues = [
            *run_reproof(
                "handoff.reap_signal",
                frozen={"signal_sha256": plan.get("signal_sha256")},
                live={"signal_sha256": digest},
            ).issues,
            *run_reproof(
                "handoff.reap_signal",
                frozen={"signal_sha256": output.get("signal_sha256")},
                live={"signal_sha256": digest},
            ).issues,
        ]
        if reissue_issues:
            raise _pph.PostPublicationHandoffError(
                ";".join(dict.fromkeys(reissue_issues))
            )
        result["reap_signal"] = True

    priority = steps.get("priority_eval") or {}
    if priority.get("status") == "completed":
        payload = (priority.get("plan") or {}).get("payload")
        output = (priority.get("receipt") or {}).get("output") or {}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
        path = Path(RESULTS_DIR) / "priority_eval.json"
        with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
            _atomic_publish_state_text(path, encoded)
            reopened = _read_regular_state_text(path, allow_missing=False)
        digest = hashlib.sha256(reopened.encode("utf-8")).hexdigest()
        # The raw write/read-back equality (reopened != encoded) is graded by
        # the same digest site over the canonical re-encoding — byte-equal
        # inputs produce byte-equal digests, so only a corrupted write/read
        # round-trip can diverge.
        reissue_issues = [
            *run_reproof(
                "handoff.priority_eval",
                frozen={
                    "payload_sha256": hashlib.sha256(
                        encoded.encode("utf-8")
                    ).hexdigest()
                },
                live={"payload_sha256": digest},
            ).issues,
            *run_reproof(
                "handoff.priority_eval",
                frozen={"payload_sha256": output.get("payload_sha256")},
                live={"payload_sha256": digest},
            ).issues,
        ]
        if reissue_issues:
            raise _pph.PostPublicationHandoffError(
                ";".join(dict.fromkeys(reissue_issues))
            )
        result["priority_eval"] = True
    return result



def _reprove_external_steps(record: dict[str, Any]) -> None:
    """Unconditionally reopen every non-operational external effect at commit.

    This is the final semantic boundary: public receipt digests can detect
    accidental damage but are not permission to trust a re-signed boolean.
    Rotation archives, log archives, tombstones, Cycle Archivist output, and
    the Git worktree are therefore re-derived from their frozen plans.
    """

    identity = record["identity"]
    version = int(identity["version"])
    source_v = int(identity["source_v"])
    steps = record["steps"]

    rotation_output = steps["archive_rotation"]["receipt"]["output"]
    rotations = rotation_output["rotations"]
    from evolution_infra import validate_archive_rotation_receipts

    if validate_archive_rotation_receipts(
        version,
        rotations,
        rotation_plan=steps["archive_rotation"]["plan"],
    ) != rotations:
        raise _pph.PostPublicationHandoffError(
            "handoff_archive_rotation_external_reproof_mismatch"
        )

    from tool_commit import (
        _converge_and_verify_reaped_target,
        _revalidate_strict_log_archives,
        _validate_pool_reap_plan,
        _verify_post_publication_worktree,
    )

    log_row = steps["log_cleanup"]
    _revalidate_strict_log_archives(
        log_row["plan"],
        log_row["receipt"]["output"]["archives"],
        expected_handoff_version=version,
        expected_publication_id=identity["publication_id"],
    )

    pool_row = steps["pool_reap"]
    _initial, target_names, _snapshot = _validate_pool_reap_plan(
        pool_row["plan"], record
    )
    pool_output = pool_row["receipt"]["output"]
    if (
        pool_output.get("required_reaps") != len(target_names)
        or pool_output.get("removed_bots") != sorted(target_names)
    ):
        raise _pph.PostPublicationHandoffError(
            "handoff_pool_reap_target_set_mismatch"
        )
    prior_proofs = {
        proof.get("bot"): proof
        for proof in pool_output.get("reap_proofs") or []
        if isinstance(proof, dict)
    }
    final_proofs = []
    # The five-field per-bot ring (version / completion commit / tombstone
    # identity) grades through the engine's pool ring site: exact values with
    # the producer's historical double-None tolerance, one shared token.
    _POOL_RING_FIELDS = (
        "version", "completion_commit_oid", "tombstone_tag",
        "tombstone_object_oid", "tombstone_commit_oid",
    )
    for name in target_names:
        proof = _converge_and_verify_reaped_target(name, record)
        prior = prior_proofs.get(name) or {}
        ring_issues = run_reproof(
            "archivist.pool_reap_ring",
            frozen={field: prior.get(field) for field in _POOL_RING_FIELDS},
            live={field: proof.get(field) for field in _POOL_RING_FIELDS},
        ).issues
        if ring_issues:
            raise _pph.PostPublicationHandoffError(
                ";".join(dict.fromkeys(ring_issues))
            )
        final_proofs.append(proof)

    archive = _pph._read_json(_pph._archive_path(version))
    annotation = archive.get("archivist_notes")
    from cycle_archivist import annotation_identity_errors

    annotation_errors = annotation_identity_errors(
        annotation,
        archive,
        version=version,
        source_v=source_v,
    )
    cycle_output = steps["cycle_annotation"]["receipt"]["output"]
    if annotation_errors:
        raise _pph.PostPublicationHandoffError(
            "handoff_cycle_annotation_external_reproof_invalid:"
            + ";".join(annotation_errors[:20])
        )
    if (
        annotation.get("annotation_digest")
        != cycle_output.get("annotation_digest")
        or _pph.archive_semantic_digest(archive)
        != cycle_output.get("archive_semantic_digest")
    ):
        raise _pph.PostPublicationHandoffError(
            "handoff_cycle_annotation_external_reproof_mismatch"
        )

    housekeeping_row = steps["housekeeping"]
    housekeeping_plan = housekeeping_row["plan"]
    expected_dependencies = {
        name: steps[name]["receipt"]["receipt_digest"]
        for name in (
            "archive_rotation", "log_cleanup", "pool_reap",
            "cycle_annotation",
        )
    }
    if housekeeping_plan.get("dependency_receipts") != expected_dependencies:
        raise _pph.PostPublicationHandoffError(
            "handoff_housekeeping_dependency_mismatch"
        )
    actual_worktree = _verify_post_publication_worktree(
        expected_head=housekeeping_plan["expected_head_oid"],
        expected_dirty=set(housekeeping_plan["expected_dirty_paths"]),
    )
    housekeeping_output = housekeeping_row["receipt"]["output"]
    from tool_commit_archivist import _publication_head_advance_allowed

    worktree_probe = _pph._SeamGitProbe(_publication_head_advance_allowed)
    # Worktree ring: every field stays strict except the frozen head, which
    # grades equal-or-ancestor (twenty-first exact-pin, v579: the frozen
    # receipt recorded HEAD at housekeeping-completion time; a legitimate
    # post-publication operator deploy advanced it and the reproof strict-
    # compared the OLD receipt head against the NEW live one and wedged the
    # finalization).  One shared token, exactly like the inline loop before.
    worktree_issues = run_reproof(
        "handoff.housekeeping",
        frozen={
            "head_oid": housekeeping_output.get("head_oid"),
            "worktree_status_digest": housekeeping_output.get(
                "worktree_status_digest"
            ),
            "tracked_housekeeping_commit": housekeeping_output.get(
                "tracked_housekeeping_commit"
            ),
        },
        live=dict(actual_worktree),
        probe=worktree_probe,
    ).issues
    if worktree_issues:
        raise _pph.PostPublicationHandoffError(
            ";".join(dict.fromkeys(worktree_issues))
        )
    # Reap proofs, per bot (U1, the whole-dict wedge): every publication-
    # bound field grades exact through the engine's per-bot contract and the
    # nested remote leg's verified tip grades under the SAME ancestor rule
    # (a legitimate operator push between housekeeping and finalization
    # advances origin past the publication-time verification).  The raw
    # reap_proof_set_digest re-derivation the old second leg performed is
    # subsumed: the receipt-internal output contract already binds the
    # stored digest to the frozen raw bytes, and re-deriving a digest over
    # live raw bytes is exactly what wedged v566.  Explicit-test-mode
    # (local-only) proofs — remote_proof {"required": False} only, which the
    # engine's nested required-remote contract can never carry — keep the
    # historical whole-record equality grade.
    def _required_remote_only(proofs: Any) -> bool:
        return all(
            isinstance(proof, dict)
            and isinstance(proof.get("remote_proof"), dict)
            and proof["remote_proof"].get("required") is True
            for proof in (proofs or [])
        )

    frozen_reap_proofs = housekeeping_output.get("reap_proofs")
    reap_site = (
        "handoff.housekeeping.reap_proofs"
        if (
            _required_remote_only(frozen_reap_proofs)
            and _required_remote_only(final_proofs)
        )
        else "handoff.housekeeping.reap_proofs_exact"
    )
    reap_issues = run_reproof(
        reap_site,
        frozen={"reap_proofs": frozen_reap_proofs or []},
        live={"reap_proofs": final_proofs},
        probe=worktree_probe,
    ).issues
    if reap_issues:
        raise _pph.PostPublicationHandoffError(
            ";".join(dict.fromkeys(reap_issues))
        )



def _step_receipt_errors(
    name: str,
    row: Any,
    identity: dict[str, Any],
) -> list[str]:
    if not isinstance(row, dict) or set(row) != {
        "status", "plan", "plan_digest", "receipt",
    }:
        return [f"handoff_step_receipt_row_shape_invalid:{name}"]
    if row.get("status") != "completed":
        return [f"handoff_step_incomplete:{name}"]
    receipt = row.get("receipt")
    if not isinstance(receipt, dict) or set(receipt) != _pph._STEP_RECEIPT_KEYS:
        return [f"handoff_step_receipt_shape_invalid:{name}"]
    errors: list[str] = []
    publication_id = str(identity.get("publication_id") or "")
    row_plan_digest = None
    if not isinstance(row.get("plan"), dict):
        errors.append(f"handoff_step_completed_plan_invalid:{name}")
    else:
        errors.extend(_pph._step_plan_contract_errors(name, row["plan"], identity))
        try:
            row_plan_digest = canonical_digest(row["plan"])
        except Exception:
            row_plan_digest = ""
        if row.get("plan_digest") != row_plan_digest:
            errors.append(
                f"handoff_step_completed_plan_digest_mismatch:{name}"
            )
    unsigned = {
        key: value for key, value in receipt.items() if key != "receipt_digest"
    }
    try:
        expected_digest = canonical_digest(unsigned)
    except Exception:
        expected_digest = ""
    if receipt.get("receipt_digest") != expected_digest:
        errors.append(f"handoff_step_receipt_digest_mismatch:{name}")
    if receipt.get("schema_version") != 1:
        errors.append(f"handoff_step_receipt_schema_mismatch:{name}")
    if receipt.get("step") != name:
        errors.append(f"handoff_step_receipt_name_mismatch:{name}")
    if receipt.get("publication_id") != publication_id:
        errors.append(f"handoff_step_receipt_publication_mismatch:{name}")
    if not _pph._finite_time(receipt.get("completed_at")):
        errors.append(f"handoff_step_receipt_time_invalid:{name}")
    if not isinstance(receipt.get("output"), dict):
        errors.append(f"handoff_step_receipt_output_invalid:{name}")
    plan_digest = receipt.get("plan_digest")
    if plan_digest is not None and not _pph._is_hex(plan_digest, 64):
        errors.append(f"handoff_step_receipt_plan_digest_invalid:{name}")
    if plan_digest != row_plan_digest:
        errors.append(f"handoff_step_receipt_plan_binding_mismatch:{name}")
    if row_plan_digest is not None and isinstance(receipt.get("output"), dict):
        if receipt["output"].get("plan_digest") != row_plan_digest:
            errors.append(f"handoff_step_output_plan_binding_mismatch:{name}")
        errors.extend(_pph._step_output_contract_errors(
            name,
            receipt["output"],
            row["plan"],
            row_plan_digest,
            identity,
        ))
    return errors



def _step_row_errors(
    name: str,
    row: Any,
    identity: dict[str, Any],
) -> list[str]:
    if not isinstance(row, dict):
        return [f"handoff_step_row_invalid:{name}"]
    status = row.get("status")
    if status == "pending":
        return [] if set(row) == {"status"} else [
            f"handoff_step_pending_shape_invalid:{name}"
        ]
    if status == "planned":
        errors = []
        if set(row) != {"status", "plan", "plan_digest"}:
            errors.append(f"handoff_step_plan_shape_invalid:{name}")
        plan = row.get("plan")
        if not isinstance(plan, dict):
            errors.append(f"handoff_step_plan_not_object:{name}")
        else:
            errors.extend(_pph._step_plan_contract_errors(name, plan, identity))
            try:
                expected = canonical_digest(plan)
            except Exception:
                expected = ""
            if row.get("plan_digest") != expected:
                errors.append(f"handoff_step_plan_digest_mismatch:{name}")
        return errors
    if status == "completed":
        return _pph._step_receipt_errors(name, row, identity)
    return [f"handoff_step_state_invalid:{name}"]


