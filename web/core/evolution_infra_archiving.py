"""Generation archiving and post-commit Archivist receipt cluster for evolution_infra.

Extracted as a cohesive business cluster from evolution_infra.py: the structured
archive snapshot written on generation completion (``archive_generation``), the
post-commit Archivist receipt construction/validation, and the retired
consume-before-work API stub.

evolution_infra.py retains thin delegate shells so external
``from evolution_infra import <name>`` sites and
``monkeypatch.setattr(evolution_infra, "<name>", ...)`` patches keep resolving.

IMPORTANT -- shared-symbol access model
---------------------------------------
The bodies reference parent-resident symbols (``_ei.ARCHIVE_DIR``, ``_ei._git``,
``_ei.get_active_bots``, ``_ei.get_bot_dir``, ``_ei.git_has_tag``, ``_ei.load_ratings``,
``log``) through ``_ei.<name>`` so test monkeypatches on the
``evolution_infra`` namespace (e.g. ``evolution_infra._git``,
``evolution_infra.ARCHIVE_DIR``) keep taking effect when the call originates
inside this companion.  References between members of *this* module are kept
as bare globals.
"""

from __future__ import annotations

import json
import logging
import os
import time

import evolution_infra as _ei  # for _ei.ARCHIVE_DIR, _ei._git, _ei.get_active_bots,
                               # _ei.get_bot_dir, _ei.git_has_tag, _ei.load_ratings, log,
                               # and the thin delegate shells that pick up test
                               # monkeypatches.

# Immutable constants / pure functions re-exported by evolution_infra.
from bot_namespace import (
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
    bot_relpath,
    bot_tag,
)

log = logging.getLogger("pok.infra")


def archive_generation(version, source_v, ckpt):
    """Create a structured archive snapshot for a completed generation.

    Writes results/archive/v{N}.json with key metrics from the pipeline state.
    """
    os.makedirs(_ei.ARCHIVE_DIR, exist_ok=True)
    snapshot = {
        "version": version,
        "source_v": source_v,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git_tag": bot_tag(version),
        "evaluation_epoch": EVALUATION_EPOCH,
        "bot_name": bot_name(version),
    }

    audit_context = (ckpt or {}).get("audit_context") or {}
    selection = audit_context.get("selection") or {}
    if (
        int(version) == FIRST_STRICT_POLICY_VERSION
        and isinstance(audit_context.get("protocol_bootstrap"), dict)
    ):
        snapshot["strength_evidence_identity"] = {
            "schema_version": 1,
            "mode": "empty_first_strict_bootstrap",
            "strength_evidence_admitted": False,
            "reason": "strict_policy_pool_empty",
        }
    else:
        evidence = selection.get("evaluation_evidence") or {}
        cutoffs = evidence.get("cutoffs") or {}
        snapshot["strength_evidence_identity"] = {
            "schema_version": 1,
            "mode": "frozen_native_evaluation",
            "strength_evidence_admitted": True,
            "generation_snapshot_manifest_digest": str(
                cutoffs.get("generation_snapshot_manifest_digest") or ""
            ),
            "cycle_manifest_digest": str(cutoffs.get("cycle_manifest_digest") or ""),
            "h2h_snapshot_manifest_digest": str(
                selection.get("h2h_snapshot_manifest_digest") or ""
            ),
            "h2h_snapshot_sha256": str(selection.get("h2h_snapshot_sha256") or ""),
            "selection_view_digest": str(evidence.get("selection_view_digest") or ""),
        }

    try:
        snapshot["git_commit"] = _ei._git("rev-parse", "--short", bot_tag(version), check=False)
    except Exception:
        pass

    ratings = _ei.load_ratings()
    p = ratings.get(bot_name(version))
    if p:
        snapshot["rating"] = {"r": round(p.r, 1), "rd": round(p.rd, 1)}

    try:
        from tool_helpers import compute_h2h_avg_winrate, _load_h2h_data
        h2h_wr = compute_h2h_avg_winrate(bot_name(version), _load_h2h_data())
        snapshot["h2h_avg_wr"] = round(h2h_wr, 4)
    except Exception:
        pass

    if ckpt:
        gate_results = ckpt.get("gate_results", {})
        if gate_results.get("review"):
            review_data = gate_results["review"]
            snapshot["review_score"] = review_data.get("quality_score", 0)
            if review_data.get("change_summary"):
                snapshot["reviewer_change_summary"] = review_data["change_summary"]
            if review_data.get("risk_areas"):
                snapshot["reviewer_risk_areas"] = review_data["risk_areas"]
        if gate_results.get("critic"):
            critic_data = gate_results["critic"]
            snapshot["critic_score"] = critic_data.get("score", 0)
            if critic_data.get("strategic_assessment"):
                snapshot["critic_data"] = critic_data
        precommit = gate_results.get("precommit_eval", {})
        if precommit:
            snapshot["precommit_eval"] = {"passed": precommit.get("passed", False)}

    try:
        diff_stat = _ei._git("diff", "--stat", f"{bot_tag(source_v)}..{bot_tag(version)}",
                         "--", bot_relpath(version) + "/", check=False)
        if diff_stat:
            last_line = diff_stat.strip().split("\n")[-1]
            snapshot["diff_stats_raw"] = last_line.strip()
    except Exception:
        pass

    snapshot["pool_size"] = len(_ei.get_active_bots())

    # commit_bot clears the active checkpoint before the advisory Archivist
    # runs. Issue one content-bound, single-use handoff so a weak controller
    # cannot replay run_archivist against an arbitrary historical bot/source.
    try:
        from bot_artifact import canonical_digest, hash_path

        receipt_payload = {
            "schema_version": "post-commit-archivist-v1",
            "version": int(version),
            "source_v": int(source_v),
            "bot_tag": bot_tag(version),
            "git_commit": _ei._git(
                "rev-parse",
                bot_tag(version),
                check=False,
            ).strip(),
            "artifact_hash": hash_path(_ei.get_bot_dir(version)),
            "issued_at": time.time(),
        }
        snapshot["post_commit_archivist_receipt"] = {
            **receipt_payload,
            "receipt_digest": canonical_digest(receipt_payload),
            "status": "pending",
        }
    except Exception as exc:
        log.error(
            "Could not issue post-commit Archivist receipt for v%s: %s",
            version,
            exc,
        )

    archive_path = _ei.ARCHIVE_DIR / f"v{version}.json"
    with open(archive_path, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    return snapshot


def _post_commit_archivist_receipt_validation(
    snapshot,
    version,
    source_v,
    *,
    require_pending=True,
):
    from bot_artifact import canonical_digest, hash_path

    if not isinstance(snapshot, dict):
        return False, "archive_snapshot_missing", None
    receipt = snapshot.get("post_commit_archivist_receipt")
    if not isinstance(receipt, dict):
        return False, "post_commit_archivist_receipt_missing", None
    if receipt.get("schema_version") != "post-commit-archivist-v1":
        return False, "post_commit_archivist_receipt_schema", receipt
    try:
        if int(receipt.get("version")) != int(version):
            return False, "post_commit_archivist_version_mismatch", receipt
        if int(receipt.get("source_v")) != int(source_v):
            return False, "post_commit_archivist_source_mismatch", receipt
    except (TypeError, ValueError):
        return False, "post_commit_archivist_identity_invalid", receipt
    payload = {
        key: receipt.get(key)
        for key in (
            "schema_version",
            "version",
            "source_v",
            "bot_tag",
            "git_commit",
            "artifact_hash",
            "issued_at",
        )
    }
    if receipt.get("receipt_digest") != canonical_digest(payload):
        return False, "post_commit_archivist_digest_mismatch", receipt
    if require_pending and receipt.get("status") != "pending":
        return False, "post_commit_archivist_receipt_consumed", receipt
    if receipt.get("bot_tag") != bot_tag(version) or not _ei.git_has_tag(version):
        return False, "post_commit_archivist_tag_mismatch", receipt
    current_commit = _ei._git("rev-parse", bot_tag(version), check=False).strip()
    if not current_commit or receipt.get("git_commit") != current_commit:
        return False, "post_commit_archivist_commit_mismatch", receipt
    try:
        if receipt.get("artifact_hash") != hash_path(_ei.get_bot_dir(version)):
            return False, "post_commit_archivist_artifact_mismatch", receipt
    except Exception as exc:
        return False, f"post_commit_archivist_artifact_error:{type(exc).__name__}", receipt
    return True, "", receipt


def validate_post_commit_archivist_receipt(version, source_v):
    """Read-only validation for the no-checkpoint runtime guard."""
    try:
        from post_publication_handoff import pending_handoff_route

        route = pending_handoff_route()
        if route.get("status") != "pending":
            return False, ";".join(
                route.get("issues") or ["post_publication_handoff_missing"]
            ), None
        if (
            int(route.get("version")) != int(version)
            or int(route.get("source_v")) != int(source_v)
        ):
            return False, "post_publication_handoff_subject_mismatch", None
        return True, "", {
            "receipt_digest": route.get("identity_digest"),
            "publication_id": route.get("publication_id"),
            "status": route.get("state"),
            "version": int(version),
            "source_v": int(source_v),
        }
    except Exception as exc:
        return False, f"post_publication_handoff_error:{type(exc).__name__}", None


def consume_post_commit_archivist_receipt(version, source_v):
    """Retired consume-before-work API; callers must use the step journal."""
    return False, "post_commit_archivist_consume_api_retired", None



