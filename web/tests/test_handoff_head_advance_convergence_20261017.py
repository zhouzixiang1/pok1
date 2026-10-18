"""Published-generation handoff convergence after HEAD advanced (2026-10-17).

v566 published completely (commit 1d4391dc, both tags pushed) and then its
post-publication handoff never converged: bug-fix deploys advanced HEAD to
2b4dafbd/d5d06871, and three frozen-record-vs-live-git comparisons kept
failing on the exact publication-time pin —
``handoff_live_local_publication_proof_mismatch`` /
``handoff_live_certificate_digest_mismatch`` (live re-proof) and
``candidate_artifact_hash_mismatch`` (the commit_bot retry identity rebuild),
plus the Archivist's exact-HEAD checks.  These tests pin the replacement
contract: only the publication-bound (stable) fields are byte-compared, and
a live drift is accepted exactly while the publication commit is an ancestor
of the current HEAD.
"""

from __future__ import annotations

import copy
import json
import types
from pathlib import Path

import pytest

from conftest import STRICT_SOURCE_V, STRICT_TARGET_V

V = STRICT_TARGET_V
S = STRICT_SOURCE_V

ADVANCED_HEAD = "f" * 40


def _proof_payload(module, *, version, main_commit_oid=None):
    commit_oid = f"{version:040x}"
    return {
        "schema_version": 1,
        "kind": "national-tcp-policy-pending-local-publication",
        "bot": module.bot_name(version),
        "version": version,
        "artifact_hash": f"{version + 3:064x}",
        "tag": module.bot_tag(version),
        "tag_object": f"{version + 1:040x}",
        "commit_oid": commit_oid,
        "completion_tree_oid": f"{version + 5:040x}",
        "main_commit_oid": commit_oid
        if main_commit_oid is None
        else main_commit_oid,
    }


def _signed_proof(module, payload):
    return {**payload, "proof_digest": module.canonical_digest(payload)}


def _write_pair(module, *, version=None, source_v=None, certificate=None):
    """Freeze one structurally valid journal pair (record+archive+pointer)."""

    if version is None:
        version = V
    if source_v is None:
        source_v = S
    publication_id = f"{version:064x}"
    commit_oid = f"{version:040x}"
    completion_object = f"{version + 1:040x}"
    high_water_object = f"{version + 2:040x}"
    artifact_hash = f"{version + 3:064x}"
    certificate_digest = (
        f"{version + 4:064x}" if certificate is None else certificate
    )
    completion_name = module.bot_tag(version)
    high_water_name = module.high_water_tag(version)
    local_proof = _signed_proof(module, _proof_payload(module, version=version))
    epoch_binding = {"fixture": version}
    projection = {
        "checkpoint_schema_version": 2,
        "evaluation_epoch": module.EVALUATION_EPOCH,
        "epoch_binding": epoch_binding,
        "next_v": version,
        "source_v": source_v,
        "parent2_v": None,
        "generation_mode": "master",
        "workflow_run_id": f"generation:{version}:handoff-test",
        "checkpoint_revision": 11,
        "stage": "publishing",
        "workflow_profile_id": "national-policy-v1",
        "national_execution_mode": "native_tcp",
        "generation_attempt": 0,
        "precommit_rework_count": 0,
        "official_rework_count": 0,
        "repair_baseline_artifact_hash": None,
        "audit_context": {},
        "publication_intent": {
            "publication_id": publication_id,
            "candidate_artifact_hash": artifact_hash,
            "official_certificate_digest": certificate_digest,
        },
    }
    source_binding = {
        "source_v": source_v,
        "parent2_v": None,
        "epoch_binding": epoch_binding,
        "protocol_bootstrap_receipt_digest": None,
    }
    publication_identity = {
        "schema_version": module.SCHEMA_VERSION,
        "evaluation_epoch": module.EVALUATION_EPOCH,
        "version": version,
        "source_v": source_v,
        "workflow_run_id": projection["workflow_run_id"],
        "checkpoint_revision": projection["checkpoint_revision"],
        "publishing_checkpoint_digest": module.canonical_digest(projection),
        "publication_id": publication_id,
        "commit_oid": commit_oid,
        "candidate_artifact_hash": artifact_hash,
        "source_binding_digest": module.canonical_digest(source_binding),
        "local_paired_refs": {
            completion_name: {
                "object_oid": completion_object,
                "peeled_commit_oid": commit_oid,
            },
            high_water_name: {
                "object_oid": high_water_object,
                "peeled_commit_oid": commit_oid,
            },
        },
        "local_publication_proof": local_proof,
        "certificate_digest": certificate_digest,
        "remote_publication": {
            "required": False,
            "explicit_test_mode": True,
            "remote_main_oid": None,
            "paired_refs": {},
        },
    }
    base = {
        "schema_version": 2,
        "kind": "national-policy-generation-archive",
        "evaluation_epoch": module.EVALUATION_EPOCH,
        "version": version,
        "source_v": source_v,
        "bot_name": module.bot_name(version),
        "git_tag": module.bot_tag(version),
        "publication_identity": publication_identity,
        "publishing_checkpoint_projection": projection,
        "publishing_checkpoint_digest": module.canonical_digest(projection),
        "strength_evidence_identity": {"fixture": version},
        "review_score": 0,
        "reviewer_change_summary": "",
        "reviewer_risk_areas": [],
        "critic_score": 0,
        "precommit_passed": True,
    }
    base_digest = module.canonical_digest(base)
    record = module._base_record(publication_identity, base_digest)
    archive = {
        **base,
        "base_snapshot_digest": base_digest,
        "post_publication_handoff": {
            "identity_digest": record["identity_digest"],
            "publication_id": publication_id,
            "state": "pending",
        },
        "finalization": {"state": "pending"},
    }
    record_path = module._handoff_path(version, publication_id)
    module._atomic_write(module._archive_path(version), archive)
    module._atomic_write(record_path, record)
    module._atomic_write(
        module._active_pointer_path(),
        module._active_pointer(record, record_path),
    )
    return record_path, record, archive


def _head_advanced_retry_identity(module, record, archive):
    """Recomputed identity of the SAME publication after HEAD advanced.

    Mirrors what a commit_bot re-entry of an already-tagged, remote-proven
    publication derives: the CAS writer bumped ``checkpoint_revision`` (the
    2b4dafbd drift) AND the rebuilt local proof carries the new branch tip as
    ``main_commit_oid`` (with its re-derived ``proof_digest``).
    """

    frozen = record["identity"]
    version = int(frozen["version"])
    drifted = {
        key: copy.deepcopy(value)
        for key, value in frozen.items()
        if key != "archive_base_snapshot_digest"
    }
    drifted["checkpoint_revision"] = frozen["checkpoint_revision"] + 1
    projection = copy.deepcopy(archive["publishing_checkpoint_projection"])
    projection["checkpoint_revision"] = drifted["checkpoint_revision"]
    drifted["publishing_checkpoint_digest"] = module.canonical_digest(projection)
    payload = _proof_payload(module, version=version, main_commit_oid=ADVANCED_HEAD)
    drifted["local_publication_proof"] = _signed_proof(module, payload)
    return drifted


@pytest.fixture
def handoff_env(tmp_path, monkeypatch):
    import evolution_infra
    import generation_evidence
    import post_publication_handoff as module
    import tool_commit

    results = tmp_path / "results"
    monkeypatch.setattr(
        evolution_infra,
        "POST_PUBLICATION_HANDOFF_DIR",
        results / "post_publication_handoffs",
    )
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(
        evolution_infra, "WORKER_FAILURES_FILE", results / "worker_failures.jsonl"
    )
    monkeypatch.setattr(
        evolution_infra, "MATCH_HISTORY_FILE", results / "match_history.jsonl"
    )
    monkeypatch.setattr(
        evolution_infra, "RATING_HISTORY_FILE", results / "rating_history.jsonl"
    )
    monkeypatch.setattr(
        evolution_infra, "LLM_COSTS_FILE", results / "llm_costs.jsonl"
    )
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", results / "archive")
    monkeypatch.setattr(tool_commit, "RESULTS_DIR", results)
    monkeypatch.setattr(tool_commit, "ARCHIVE_DIR", results / "archive")
    monkeypatch.setenv(
        "POK_ALLOW_LOCAL_ONLY_POST_PUBLICATION_HANDOFF_FOR_TESTS", "1"
    )
    monkeypatch.setattr(
        generation_evidence,
        "generation_evidence_identity_errors",
        lambda *_args, **_kwargs: [],
    )
    with module._ACTIVE_CLAIMS_LOCK:
        module._ACTIVE_CLAIMS.clear()
    yield module, results, tmp_path, monkeypatch
    with module._ACTIVE_CLAIMS_LOCK:
        module._ACTIVE_CLAIMS.clear()


# ─────────────────────────────────────────────────────────────────────────
# Fix 3 — the commit_bot retry identity rebuild
# ─────────────────────────────────────────────────────────────────────────


def _retry_publication_result(module, *, version, main_commit_oid):
    commit_oid = f"{version:040x}"
    completion_name = module.bot_tag(version)
    high_water_name = module.high_water_tag(version)
    refs = {}
    for name, offset in ((completion_name, 1), (high_water_name, 2)):
        refs[name] = {
            "type": "tag",
            "object_oid": f"{version + offset:040x}",
            "peeled_commit_oid": commit_oid,
        }
    return {
        "committed": True,
        "publication_id": f"{version:064x}",
        "commit_oid": commit_oid,
        "local_refs": refs,
        "local_publication_proof": _signed_proof(
            module,
            _proof_payload(
                module, version=version, main_commit_oid=main_commit_oid
            ),
        ),
        "remote_proof": {"valid": True, "local_only": True},
    }


def _retry_checkpoint(module, *, version=None, source_v=None):
    if version is None:
        version = V
    if source_v is None:
        source_v = S
    return {
        "checkpoint_schema_version": 2,
        "evaluation_epoch": module.EVALUATION_EPOCH,
        "epoch_binding": {"fixture": version},
        "next_v": version,
        "source_v": source_v,
        "parent2_v": None,
        "generation_mode": "master",
        "workflow_run_id": f"generation:{version}:handoff-test",
        "checkpoint_revision": 11,
        "stage": "publishing",
        "workflow_profile_id": "national-policy-v1",
        "national_execution_mode": "native_tcp",
        "generation_attempt": 0,
        "precommit_rework_count": 0,
        "official_rework_count": 0,
        "repair_baseline_artifact_hash": None,
        "audit_context": {},
        "publication_intent": {
            "publication_id": f"{version:064x}",
            "candidate_artifact_hash": f"{version + 3:064x}",
            "official_certificate_digest": f"{version + 4:064x}",
        },
        "repo_baseline": {"head": f"{version:040x}"},
    }


def test_retry_identity_rebuild_accepts_advanced_main_commit_oid(
    handoff_env, monkeypatch
):
    """Fix 3: a rebuilt proof with a drifted main_commit_oid is the same
    publication when the commit is an ancestor of HEAD, and only then."""

    module, _results, _tmp, _mp = handoff_env
    checkpoint = _retry_checkpoint(module)
    ancestor = {"allowed": True}
    monkeypatch.setattr(
        module,
        "_publication_commit_is_ancestor_of",
        lambda _oid, _tip: ancestor["allowed"],
    )

    def rebuild(main_commit_oid):
        return module._publication_identity(
            version=V,
            source_v=S,
            checkpoint=checkpoint,
            publication_result=_retry_publication_result(
                module, version=V, main_commit_oid=main_commit_oid
            ),
            allow_local_only=True,
        )

    # First publication: HEAD IS the publication commit.
    exact = rebuild(f"{V:040x}")
    assert exact["local_publication_proof"]["main_commit_oid"] == f"{V:040x}"

    # Retry after a deploy advanced HEAD: the drift is accepted under the
    # ancestor rule instead of raising candidate_artifact_hash_mismatch.
    drifted = rebuild(ADVANCED_HEAD)
    assert drifted["local_publication_proof"]["main_commit_oid"] == ADVANCED_HEAD

    # A main commit that is NOT on the publication line still fails closed.
    ancestor["allowed"] = False
    with pytest.raises(
        module.PostPublicationHandoffError,
        match="candidate_artifact_hash_mismatch",
    ):
        rebuild(ADVANCED_HEAD)


def test_spine_adoption_survives_head_advance_drift(handoff_env, monkeypatch):
    """Fix 3 (spine): adoption compares stable proof fields and grades the
    main_commit_oid/proof_digest drift by the ancestor rule."""

    module, _results, _tmp, _mp = handoff_env
    _path, record, archive = _write_pair(module)
    drifted = _head_advanced_retry_identity(module, record, archive)
    assert (
        drifted["local_publication_proof"]["main_commit_oid"]
        != record["identity"]["local_publication_proof"]["main_commit_oid"]
    )

    ancestor = {"allowed": True}
    monkeypatch.setattr(
        module,
        "_publication_commit_is_ancestor_of",
        lambda _oid, _tip: ancestor["allowed"],
    )
    assert module._same_publication_identity(record["identity"], drifted)

    # A non-ancestor branch line is a different publication: fail closed.
    ancestor["allowed"] = False
    assert not module._same_publication_identity(record["identity"], drifted)

    # Forged stable fields never adopt, even with the ancestor rule green.
    ancestor["allowed"] = True
    forged_tag_object = copy.deepcopy(drifted)
    payload = {
        key: value
        for key, value in forged_tag_object["local_publication_proof"].items()
        if key != "proof_digest"
    }
    payload["tag_object"] = "9" * 40
    forged_tag_object["local_publication_proof"] = _signed_proof(module, payload)
    assert not module._same_publication_identity(
        record["identity"], forged_tag_object
    )
    forged_artifact = copy.deepcopy(drifted)
    forged_artifact["candidate_artifact_hash"] = "8" * 64
    assert not module._same_publication_identity(
        record["identity"], forged_artifact
    )

    # The 2b4dafbd semantics are preserved: any other stable identity field
    # divergence (e.g. a different commit) still rejects.
    different_commit = copy.deepcopy(drifted)
    different_commit["commit_oid"] = f"{V + 42:040x}"
    assert not module._same_publication_identity(
        record["identity"], different_commit
    )


def test_frozen_record_with_advanced_main_commit_oid_stays_valid(handoff_env):
    """Fix 3 (frozen shape): a frozen proof may carry the advanced branch tip
    (creation after a deploy); only a malformed oid is structurally invalid."""

    module, _results, _tmp, _mp = handoff_env
    _path, record, _archive = _write_pair(module)
    advanced = copy.deepcopy(record)
    payload = _proof_payload(module, version=V, main_commit_oid=ADVANCED_HEAD)
    advanced["identity"]["local_publication_proof"] = _signed_proof(
        module, payload
    )
    advanced["identity_digest"] = module.canonical_digest(advanced["identity"])
    advanced["record_digest"] = module._record_digest(advanced)
    assert module.validate_handoff_record(advanced, reopen_archive=False) == []

    payload["main_commit_oid"] = "not-a-commit"
    malformed = copy.deepcopy(advanced)
    malformed["identity"]["local_publication_proof"] = _signed_proof(
        module, payload
    )
    malformed["identity_digest"] = module.canonical_digest(
        malformed["identity"]
    )
    malformed["record_digest"] = module._record_digest(malformed)
    errors = module.validate_handoff_record(malformed, reopen_archive=False)
    assert "handoff_local_publication_proof_identity_mismatch" in errors


# ─────────────────────────────────────────────────────────────────────────
# Fix 1 + Fix 2 — the live local re-proof
# ─────────────────────────────────────────────────────────────────────────


def _install_live_reproof(
    monkeypatch,
    tmp_path,
    *,
    fresh_proof,
    spec_certificate,
    spec_eligible=True,
):
    """Fake the live surfaces ``local_handoff_identity_errors`` opens."""

    import bot_namespace
    import evolution_infra
    import national_runtime_authority

    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        national_runtime_authority,
        "build_pending_local_publication_proof",
        lambda _path: dict(fresh_proof),
    )
    monkeypatch.setattr(
        bot_namespace,
        "resolve_national_bot_spec",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            eligible=spec_eligible,
            issues=[],
            certificate_digest=spec_certificate,
        ),
    )


def _tag_git(record):
    """Fake ``_git`` answering the frozen paired-ref tag queries exactly."""

    identity = record["identity"]
    answers = {}
    for name, row in identity["local_paired_refs"].items():
        answers[f"refs/tags/{name}"] = row["object_oid"]
        answers[f"refs/tags/{name}^{{commit}}"] = identity["commit_oid"]

    def git(*args, **_kwargs):
        if args[0] == "cat-file":
            return "tag"
        if args[0] == "rev-parse":
            return answers.get(args[1], "")
        return ""

    return git


@pytest.mark.parametrize(
    "frozen_certificate,spec_certificate,expect_error",
    [
        # None / empty-string are the same "no certificate" spelling on the
        # two sides of the comparison (spec None vs frozen "").
        ("", None, False),
        ("", "", False),
        # A real digest must match byte-for-byte…
        (f"{V + 4:064x}", f"{V + 4:064x}", False),
        # …and any real disagreement still rejects.
        (f"{V + 4:064x}", None, True),
        (f"{V + 4:064x}", "7" * 64, True),
        ("", f"{V + 4:064x}", True),
    ],
)
def test_certificate_reproof_normalizes_none_and_empty_spellings(
    handoff_env, frozen_certificate, spec_certificate, expect_error
):
    """Fix 1: spec None vs frozen '' no longer rejects a cert-less bot."""

    module, _results, tmp_path, monkeypatch = handoff_env
    _path, record, _archive = _write_pair(
        module, certificate=frozen_certificate
    )
    fresh_proof = dict(record["identity"]["local_publication_proof"])

    import evolution_infra

    monkeypatch.setattr(evolution_infra, "_git", _tag_git(record))
    _install_live_reproof(
        monkeypatch,
        tmp_path,
        fresh_proof=fresh_proof,
        spec_certificate=spec_certificate,
    )

    errors = module.local_handoff_identity_errors(record)
    has_error = any(
        "handoff_live_certificate_digest_mismatch" in item for item in errors
    )
    assert has_error is expect_error, errors


def test_live_proof_reproof_grades_stable_fields_and_head_advance(
    handoff_env, monkeypatch
):
    """Fix 2: the re-proof compares stable fields; main_commit_oid drift is
    accepted only under the ancestor rule."""

    module, _results, tmp_path, _mp = handoff_env
    _path, record, _archive = _write_pair(module)
    frozen_proof = record["identity"]["local_publication_proof"]

    import evolution_infra

    monkeypatch.setattr(evolution_infra, "_git", _tag_git(record))
    ancestor = {"allowed": True}
    monkeypatch.setattr(
        module,
        "_publication_commit_is_ancestor_of",
        lambda _oid, _tip: ancestor["allowed"],
    )

    def errors_for(fresh_proof):
        _install_live_reproof(
            monkeypatch,
            tmp_path,
            fresh_proof=fresh_proof,
            spec_certificate=record["identity"]["certificate_digest"],
        )
        return module.local_handoff_identity_errors(record)

    # Exact retry: no drift, no error.
    assert errors_for(dict(frozen_proof)) == []

    # HEAD advanced past the publication: main_commit_oid/proof_digest drift
    # re-derives and is accepted while the commit is an ancestor of HEAD.
    payload = _proof_payload(module, version=V, main_commit_oid=ADVANCED_HEAD)
    assert errors_for(_signed_proof(module, payload)) == []

    # Same drift on a non-ancestor branch line still rejects.
    ancestor["allowed"] = False
    drift_errors = errors_for(_signed_proof(module, payload))
    assert any(
        "handoff_live_local_publication_proof_mismatch" in item
        for item in drift_errors
    )

    # A forged stable field rejects even with the ancestor rule green.
    ancestor["allowed"] = True
    forged = _proof_payload(module, version=V, main_commit_oid=ADVANCED_HEAD)
    forged["tag_object"] = "9" * 40
    forged_errors = errors_for(_signed_proof(module, forged))
    assert any(
        "handoff_live_local_publication_proof_mismatch" in item
        for item in forged_errors
    )


# ─────────────────────────────────────────────────────────────────────────
# Fix 4 — Archivist HEAD ancestor rule
# ─────────────────────────────────────────────────────────────────────────


def test_worktree_verification_accepts_only_ancestor_head_advance(
    tmp_path, monkeypatch
):
    import tool_commit

    calls = []

    def git(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("rev-parse", "HEAD"):
            return ADVANCED_HEAD
        if args[:2] == ("status", "--porcelain"):
            return ""
        return ""

    monkeypatch.setattr(tool_commit, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(tool_commit, "_git", git)
    monkeypatch.setattr(tool_commit, "_git_ensure_main_branch", lambda: None)

    allowed = {"value": True}

    def ancestor_rule(publication, head):
        calls.append(("ancestor", publication, head))
        return allowed["value"]

    monkeypatch.setattr(
        tool_commit, "_publication_head_advance_allowed", ancestor_rule
    )

    # Exact HEAD match never consults the ancestor rule.
    exact = tool_commit._verify_post_publication_worktree(
        expected_head=ADVANCED_HEAD,
        expected_dirty=set(),
    )
    assert exact["head_oid"] == ADVANCED_HEAD
    assert not any(item and item[0] == "ancestor" for item in calls)

    # An ancestor advance passes and reports the observed (advanced) HEAD.
    verified = tool_commit._verify_post_publication_worktree(
        expected_head="e" * 40,
        expected_dirty=set(),
    )
    assert verified["head_oid"] == ADVANCED_HEAD
    assert ("ancestor", "e" * 40, ADVANCED_HEAD) in calls

    # A non-ancestor HEAD still fails closed.
    allowed["value"] = False
    with pytest.raises(RuntimeError, match="post_publication_head_changed"):
        tool_commit._verify_post_publication_worktree(
            expected_head="e" * 40,
            expected_dirty=set(),
        )

    # The worktree-clean re-proof is NOT relaxed by the ancestor rule.
    allowed["value"] = True

    def dirty_git(*args, **_kwargs):
        if args[:2] == ("status", "--porcelain"):
            return "?? web/core/unrelated-runtime-state\n"
        return git(*args, **_kwargs)

    monkeypatch.setattr(tool_commit, "_git", dirty_git)
    with pytest.raises(RuntimeError, match="post_publication_worktree_changed"):
        tool_commit._verify_post_publication_worktree(
            expected_head="e" * 40,
            expected_dirty=set(),
        )


@pytest.mark.asyncio
# Mechanics-only: the eight step bodies (INCLUDING stability step 1) are
# stubbed here; the REAL stability preflight behaviour is covered by
# test_preflight_head_issues_tolerates_only_ancestral_advance against a real
# git graph (red-team 2026-10-17 finding 2 — a stubbed step 1 previously
# masked the fourth exact-comparison validator).
async def test_archivist_coroutine_mechanics_with_stubbed_steps(
    tmp_path, monkeypatch
):
    """Eight-step completion with HEAD advanced past the publication commit."""

    import cycle_archivist
    import evolution_infra
    import pool_ratchet
    import post_publication_handoff as handoff
    import stability_observation
    import tool_commit
    from bot_artifact import canonical_digest
    from bot_namespace import bot_name, bot_tag

    results = tmp_path / "results"
    archive_dir = results / "archive"
    results.mkdir()
    archive_dir.mkdir()
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(tool_commit, "RESULTS_DIR", results)
    monkeypatch.setattr(tool_commit, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(tool_commit, "MAX_ACTIVE_BOTS", 8)
    monkeypatch.setattr(pool_ratchet, "pool_target_size", lambda: 8)
    monkeypatch.setattr(tool_commit, "get_active_bots", lambda: [bot_name(V)])
    import tool_bot_management

    (tool_bot_management.RESULTS_DIR / "priority_eval.json").write_text(
        json.dumps({"bot": bot_name(V)}), encoding="utf-8"
    )
    monkeypatch.setattr(tool_commit, "_git_ensure_main_branch", lambda: None)

    def git(*args, **_kwargs):
        if args[:2] == ("rev-parse", "HEAD"):
            # HEAD advanced past the publication commit ("e" * 40 below).
            return ADVANCED_HEAD
        if args[:2] == ("status", "--porcelain"):
            return ""
        return ""

    monkeypatch.setattr(tool_commit, "_git", git)

    ancestor_calls = []
    allowed = {"value": True}

    def ancestor_rule(publication, head):
        ancestor_calls.append((publication, head))
        return allowed["value"]

    monkeypatch.setattr(
        tool_commit, "_publication_head_advance_allowed", ancestor_rule
    )

    steps = {name: {"status": "pending"} for name in handoff.REQUIRED_STEPS}
    record = {
        "state": "pending",
        "identity_digest": "d" * 64,
        "identity": {
            "version": V,
            "source_v": S,
            "publication_id": "a" * 64,
            "commit_oid": "e" * 40,
            "publishing_checkpoint_digest": "f" * 64,
            "remote_publication": {
                "required": False,
                "explicit_test_mode": True,
                "remote_main_oid": None,
                "paired_refs": {},
            },
        },
        "steps": steps,
    }
    snapshot = {
        "evaluation_epoch": "national_tcp_policy_v1",
        "version": V,
        "source_v": S,
        "bot_name": bot_name(V),
        "git_tag": bot_tag(V),
        "publication_identity": {
            "version": V,
            "source_v": S,
            "publication_id": "a" * 64,
            "commit_oid": "e" * 40,
            "candidate_artifact_hash": "c" * 64,
        },
        "publishing_checkpoint_projection": {"stage": "publishing"},
        "strength_evidence_identity": {"mode": "test"},
        "post_publication_handoff": {
            "identity_digest": "d" * 64,
            "publication_id": "a" * 64,
            "state": "running",
        },
        "review_score": 9,
        "critic_score": 8,
        "precommit_passed": True,
    }

    def claim(_version, _source):
        record["state"] = "running"
        return record, "claim-1"

    def plan(_version, _source, _claim, step, payload):
        record["steps"][step] = {
            "status": "planned",
            "plan": payload,
            "plan_digest": canonical_digest(payload),
        }
        return record

    def complete_step(_version, _source, _claim, step, output):
        previous = record["steps"][step]
        completed = {
            "status": "completed",
            "receipt": {
                "output": output,
                "receipt_digest": canonical_digest(
                    {"step": step, "output": output}
                ),
            },
        }
        if previous.get("status") == "planned":
            completed["plan"] = previous["plan"]
            completed["plan_digest"] = previous["plan_digest"]
        record["steps"][step] = completed
        return record

    def release(_version, _source, _claim, *, error=None):
        record["state"] = "pending"
        record["last_error"] = error

    def complete_all(_version, _source, _claim):
        assert all(row["status"] == "completed" for row in record["steps"].values())
        record["state"] = "completed"
        return record

    monkeypatch.setattr(handoff, "claim_post_publication_handoff", claim)
    monkeypatch.setattr(handoff, "plan_handoff_step", plan)
    monkeypatch.setattr(handoff, "complete_handoff_step", complete_step)
    monkeypatch.setattr(handoff, "release_post_publication_handoff_claim", release)
    monkeypatch.setattr(handoff, "complete_post_publication_handoff", complete_all)
    monkeypatch.setattr(handoff, "load_archive_snapshot", lambda _version: snapshot)

    def write_annotation(_version, _source, _claim, annotation):
        snapshot["archivist_notes"] = annotation
        return {
            "annotation_digest": annotation["annotation_digest"],
            "archive_semantic_digest": "1" * 64,
        }

    monkeypatch.setattr(handoff, "write_archive_annotation", write_annotation)
    monkeypatch.setattr(handoff, "local_handoff_identity_errors", lambda _r: [])
    monkeypatch.setattr(
        stability_observation,
        "record_published_generation",
        lambda **_kwargs: {
            "continuity_id": "continuity",
            "count": 1,
            "target": 10,
            "complete": False,
        },
    )
    monkeypatch.setattr(tool_commit, "archive_rotate_files", lambda _v, _p: [])
    monkeypatch.setattr(
        tool_commit, "_execute_strict_log_cleanup", lambda _p, **_k: []
    )
    monkeypatch.setattr(
        tool_commit,
        "_durable_archivist_state_write",
        lambda _path, _payload: "2" * 64,
    )
    monkeypatch.setattr(
        cycle_archivist,
        "_offline_cycle_input_errors",
        lambda *_args, **_kwargs: [],
    )

    async def annotate(*_args, **_kwargs):
        payload = {
            "schema_version": 1,
            "kind": "national-policy-cycle-annotation",
            "subject": {
                "epoch": "national_tcp_policy_v1",
                "version": V,
                "source_v": S,
                "bot": bot_name(V),
                "tag": bot_tag(V),
                "artifact_hash": "c" * 64,
                "strength_evidence_identity": {"mode": "test"},
            },
            "status": "annotated",
            "issues": [],
            "analysis": {
                "generation_assessment": "neutral",
                "archive_notes": "Head-advance convergence fixture.",
            },
        }
        return {**payload, "annotation_digest": canonical_digest(payload)}

    monkeypatch.setattr(cycle_archivist, "run_cycle_archivist_analysis", annotate)

    # housekeeping uses the REAL _verify_post_publication_worktree (fake git
    # above), so its ancestor acceptance is exercised end-to-end too.
    result = await tool_commit._run_durable_post_publication_archivist(V, S)
    assert result["archivist_completed"] is True
    assert ("e" * 40, ADVANCED_HEAD) in ancestor_calls

    # A non-ancestor HEAD wedges closed at the entry boundary instead.
    record["steps"] = {
        name: {"status": "pending"} for name in handoff.REQUIRED_STEPS
    }
    record["state"] = "pending"
    allowed["value"] = False
    blocked = await tool_commit._run_durable_post_publication_archivist(V, S)
    assert blocked["error"] == "POST_PUBLICATION_ARCHIVIST_PENDING"
    assert "post_publication_head_not_publication_commit" in blocked["detail"]


# ─────────────────────────────────────────────────────────────────────────
# End to end — commit_bot re-entry adopts the frozen record, clears, and
# leaves the adopted journal ready for the eight steps.
# ─────────────────────────────────────────────────────────────────────────


def test_commit_bot_reentry_adopts_frozen_record_after_head_advance(
    handoff_env, monkeypatch
):
    """The v566 shape: fully published (tag created, local-only remote proof
    in explicit test mode), checkpoint still at ``publishing`` with one
    revision bump, HEAD advanced past the publication commit.  The re-entry
    path is ``_resume_publication_transaction`` — the exact function a fenced
    retry dispatches — and no outer Worker-journal state gates it."""

    module, _results, _tmp, _mp = handoff_env
    record_path, record, archive = _write_pair(module)
    drifted = _head_advanced_retry_identity(module, record, archive)

    import national_runtime_authority
    import publication_transaction
    import tool_commit
    import tool_commit_publication
    from publication_transaction import PUBLICATION_INTENT_KIND_STAGING

    monkeypatch.setattr(
        module,
        "_publication_commit_is_ancestor_of_head",
        lambda _oid: True,
    )
    candidate = Path("/tmp/does-not-need-to-exist")
    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _v: candidate)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_a, **_k: {"missing_gates": [], "failed_gates": []},
    )
    monkeypatch.setattr(
        publication_transaction,
        "publication_gate_ledger_digest",
        lambda _ledger: "g" * 64,
    )
    monkeypatch.setattr(
        publication_transaction,
        "publication_intent_live_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        publication_transaction,
        "publication_intent_checkpoint_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda **_k: (module.bot_name(V),),
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "build_pending_local_publication_proof",
        lambda _path: dict(drifted["local_publication_proof"]),
    )
    monkeypatch.setattr(tool_commit, "evolution_git_push_required", lambda: False)
    monkeypatch.setattr(
        tool_commit,
        "_write_completed_sentinel_durable",
        lambda _bot_dir, _pid: None,
    )
    commit_oid = record["identity"]["commit_oid"]
    monkeypatch.setattr(
        tool_commit,
        "ensure_bot_git_publication",
        lambda *_a, **_k: {
            "commit_oid": commit_oid,
            "push_ok": False,
            "local_refs": {
                name: {**row, "type": "tag"}
                for name, row in record["identity"]["local_paired_refs"].items()
            },
        },
    )

    checkpoint = _retry_checkpoint(module)
    checkpoint["checkpoint_revision"] = drifted["checkpoint_revision"]
    intent = dict(
        archive["publishing_checkpoint_projection"]["publication_intent"]
    )
    intent["kind"] = PUBLICATION_INTENT_KIND_STAGING
    intent["remote_publication_required"] = False
    intent["remote_publication_enabled"] = False
    checkpoint["publication_intent"] = intent
    checkpoint["repo_baseline"] = {"head": commit_oid}
    monkeypatch.setattr(
        tool_commit, "read_pipeline_checkpoint", lambda: checkpoint
    )
    clear_calls = []

    def clear(**kwargs):
        clear_calls.append(kwargs)
        return True

    monkeypatch.setattr(tool_commit, "clear_pipeline_checkpoint", clear)

    result = tool_commit_publication._resume_publication_transaction(
        V, S, checkpoint
    )

    # The retry converged: committed, checkpoint cleared against the LIVE
    # post-drift revision, and the frozen handoff record adopted byte-exact.
    assert result["committed"] is True
    assert result["checkpoint_cleared"] is True
    assert (
        result["post_publication_handoff_identity_digest"]
        == record["identity_digest"]
    )
    assert clear_calls[-1]["expected_checkpoint_stage"] == "publishing"
    assert clear_calls[-1]["expected_checkpoint_revision"] == drifted[
        "checkpoint_revision"
    ]
    assert module._read_json(record_path) == record
    pointer = module._read_json(module._active_pointer_path())
    assert pointer["identity_digest"] == record["identity_digest"]
    # The route probe re-opens the live local identity (real bot dir / git
    # refs); its head-advance grading is covered by the dedicated re-proof
    # tests above, so stub it here exactly like the existing journal suite.
    monkeypatch.setattr(
        module, "local_handoff_identity_errors", lambda _record: []
    )
    route = module.pending_handoff_route()
    assert route["status"] == "pending"
    assert route["record"]["identity_digest"] == record["identity_digest"]

    # Without the ancestor rule the same retry wedges before adoption: the
    # rebuilt proof fails the identity shape and never reaches the adoption
    # branch (the pre-fix v566 barrier).
    monkeypatch.setattr(
        module, "_publication_commit_is_ancestor_of_head", lambda _oid: False
    )
    wedged = tool_commit_publication._resume_publication_transaction(
        V, S, checkpoint
    )
    assert wedged["committed"] is False
    assert "candidate_artifact_hash_mismatch" in str(wedged.get("reason"))
    assert module._read_json(record_path) == record


def _real_git_repo(tmp_path, monkeypatch):
    """A real git repo whose HEAD has advanced past the publication commit."""
    import subprocess

    import evolution_infra

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    def _git(*args):
        subprocess.run(
            ["git", *args],
            cwd=str(repo),
            env={**env, "PATH": "/usr/bin:/bin:/usr/local/bin"},
            check=True,
            capture_output=True,
        )

    _git("init", "-q")
    (repo / "f.txt").write_text("base")
    _git("add", "-A")
    _git("commit", "-qm", "publication")
    pub = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / "f.txt").write_text("advanced")
    _git("add", "-A")
    _git("commit", "-qm", "post-publication deploy")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git("branch", "alt")
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", repo)
    return repo, pub, head


def test_ancestor_predicates_against_real_git(tmp_path, monkeypatch):
    # Red-team 2026-10-17 finding 2: every ancestor predicate in the new
    # tests was mocked; these exercise the REAL implementations against a
    # real git graph.
    import post_publication_handoff as handoff
    import tool_commit_archivist

    repo, pub, head = _real_git_repo(tmp_path, monkeypatch)

    assert handoff._publication_commit_is_ancestor_of_head(pub) is True
    assert handoff._publication_commit_is_ancestor_of_head(head) is True
    # A non-commit string fails closed without touching git.
    assert handoff._publication_commit_is_ancestor_of_head("garbage") is False
    assert (
        tool_commit_archivist._publication_head_advance_allowed(pub, head)
        is True
    )
    assert (
        tool_commit_archivist._publication_head_advance_allowed(head, pub)
        is False
    )
    assert (
        tool_commit_archivist._publication_head_advance_allowed(
            "ZZ", head
        )
        is False
    )


def test_preflight_head_issues_tolerates_only_ancestral_advance(
    tmp_path, monkeypatch
):
    # Red-team 2026-10-17 finding 1: the stability step-1 preflight must
    # tolerate a HEAD that advanced past the publication commit (v566
    # convergence) while still failing closed for non-ancestral tips.
    import stability_observation

    repo, pub, head = _real_git_repo(tmp_path, monkeypatch)

    # Exact pin still passes untouched.
    assert (
        stability_observation._preflight_head_issues(
            pub, current_head=pub, remote_main=pub
        )
        == []
    )
    # HEAD advanced, remote still at the publication commit: the repository
    # leg is tolerated under the ancestor rule, remote has no mismatch.
    assert (
        stability_observation._preflight_head_issues(
            pub, current_head=head, remote_main=pub
        )
        == []
    )
    # HEAD advanced AND remote advanced (both ancestors of the same line):
    # both mismatches tolerated.
    assert (
        stability_observation._preflight_head_issues(
            pub, current_head=head, remote_main=head
        )
        == []
    )
    # A NON-ancestral tip (the publication commit is not an ancestor of the
    # alt-branch tip built from the publication commit's parent... here alt
    # shares history, so instead prove the negative with a reversed pair):
    # current_head = pub while HEAD in the repo is ahead is handled above;
    # a garbage publication commit fails closed with the mismatch intact.
    assert stability_observation._preflight_head_issues(
        "not-a-commit", current_head=head, remote_main=head
    ) == [
        "publication_repository_head_mismatch",
        "publication_remote_main_head_mismatch",
    ]


def _remote_proof(commit_oid: str, remote_main: str) -> dict:
    return {
        "valid": True,
        "version": 7,
        "publication_id": "p" * 64,
        "commit_oid": commit_oid,
        "remote_main_oid": remote_main,
        "remote_refs": {"refs/tags/x": {"object_oid": "1" * 40, "peeled": "2" * 40}},
        "issues": [],
    }


def _remote_proof_legacy(commit_oid: str, remote_main: str) -> dict:
    """The intent-shaped era of the remote proof (required/paired_refs)."""
    return {
        "required": False,
        "explicit_test_mode": True,
        "remote_main_oid": remote_main,
        "paired_refs": {},
    }


def test_same_remote_publication_tolerates_only_ancestral_main_drift(
    tmp_path, monkeypatch
):
    # Root cause A (2026-10-18): the frozen remote tip observed at publication
    # time legitimately advances past the publication commit when a
    # post-publication deploy is pushed; adoption must not byte-compare it.
    import post_publication_handoff as handoff

    repo, pub, head = _real_git_repo(tmp_path, monkeypatch)
    frozen = _remote_proof(pub, pub)
    advanced = _remote_proof(pub, head)
    stale = _remote_proof(pub, "0" * 40)

    # Exact match passes.
    assert handoff._same_remote_publication(frozen, frozen, commit_oid=pub)
    # Remote advanced past the publication commit: ancestor rule accepts.
    assert handoff._same_remote_publication(frozen, advanced, commit_oid=pub)
    # Reversed (frozen ahead of recomputed): the publication commit is not an
    # ancestor of the stale zero tip — fails closed.
    assert not handoff._same_remote_publication(advanced, stale, commit_oid=pub)
    # Stable-field forgery is rejected even with an ancestral main.
    forged = dict(advanced)
    forged["remote_refs"] = {"refs/tags/x": {"object_oid": "9" * 40}}
    assert not handoff._same_remote_publication(frozen, forged, commit_oid=pub)
    forged_commit = dict(advanced)
    forged_commit["commit_oid"] = "3" * 40
    assert not handoff._same_remote_publication(frozen, forged_commit, commit_oid=pub)
    # Non-dict / key-set drift fail.
    assert not handoff._same_remote_publication(None, advanced, commit_oid=pub)
    missing = {k: v for k, v in advanced.items() if k != "remote_refs"}
    assert not handoff._same_remote_publication(frozen, missing, commit_oid=pub)
    # The legacy intent-shaped proof grades identically (key-set stability).
    legacy_frozen = _remote_proof_legacy(pub, pub)
    legacy_advanced = _remote_proof_legacy(pub, head)
    assert handoff._same_remote_publication(
        legacy_frozen, legacy_advanced, commit_oid=pub
    )
    assert not handoff._same_remote_publication(
        legacy_frozen, advanced, commit_oid=pub
    )


def test_precommit_plan_frozen_evaluator_identity_toggle():
    # Root cause B (2026-10-18): inside the fully proven publication window
    # the plan's own evaluator identity is authoritative; outside it the live
    # SEMANTIC_PATHS drift stays a hard gate.
    import precommit_eval_contract as contract

    plan = {
        "schema_version": contract.PLAN_SCHEMA_VERSION,
        "candidate_version": 7,
        "source_version": 6,
        "profile_id": "pid",
        "execution_mode": "emode",
        "evaluation_protocol": "proto",
        "evaluator_identity": "stale-identity",
        "opponents": [{"name": "national_cloud_v1"}],
        "require_published_opponents": True,
    }
    plan["plan_digest"] = contract.canonical_digest(
        {k: v for k, v in plan.items() if k != "plan_digest"}
    )
    kwargs = dict(
        candidate_version=7,
        source_version=6,
        profile_id="pid",
        execution_mode="emode",
        evaluation_protocol="proto",
    )
    strict = contract.validate_precommit_plan(plan, **kwargs)
    assert "precommit_evaluator_identity_drift" in strict
    windowed = contract.validate_precommit_plan(
        plan, frozen_evaluator_identity=True, **kwargs
    )
    assert "precommit_evaluator_identity_drift" not in windowed


def test_housekeeping_output_contract_accepts_only_ancestor_head(
    tmp_path, monkeypatch
):
    # Eleventh exact-pin (2026-10-18): the housekeeping receipt records the
    # head actually observed at verification time (_verify_post_publication_
    # worktree returns the live HEAD), which legitimately advanced past the
    # publication commit when a post-publication bug-fix deploy landed.  The
    # output contract must grade with the same ancestor rule the executor
    # already applies; a non-ancestral observed head still fails closed.
    import post_publication_handoff_step_contracts as contracts

    repo, pub, head = _real_git_repo(tmp_path, monkeypatch)

    # A parentless orphan commit: well-formed 40-hex, provably NOT a
    # descendant of the publication commit.
    import subprocess

    orphan = subprocess.run(
        ["git", "commit-tree", f"{head}^{{tree}}", "-m", "orphan"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    identity = {
        "publication_id": "pub-1",
        "commit_oid": pub,
        "version": 566,
    }
    plan = {
        "schema_version": 1,
        "kind": "post-publication-worktree-verification-plan",
        "expected_head_oid": pub,
        "expected_dirty_paths": [],
        "tracked_housekeeping_commit_allowed": False,
        "dependency_receipts": {
            name: "a" * 64
            for name in (
                "archive_rotation",
                "log_cleanup",
                "pool_reap",
                "cycle_annotation",
            )
        },
    }
    plan_digest = "b" * 64

    def output_for(head_oid):
        proofs: list = []
        return {
            "plan_digest": plan_digest,
            "head_oid": head_oid,
            "worktree_status_digest": "c" * 64,
            "tracked_housekeeping_commit": False,
            "archive_rotation_revalidated": True,
            "strict_log_archives_revalidated": True,
            "reap_proofs": proofs,
            "reap_proof_set_digest": contracts.canonical_digest(proofs),
        }

    # Exact pin (no deploy) passes untouched.
    assert contracts._step_output_contract_errors(
        "housekeeping", output_for(pub), plan, plan_digest, identity
    ) == []
    # Observed head advanced past the publication commit: accepted under the
    # REAL merge-base ancestor rule.
    assert contracts._step_output_contract_errors(
        "housekeeping", output_for(head), plan, plan_digest, identity
    ) == []
    # A well-formed non-ancestral head fails closed with the identity error.
    assert contracts._step_output_contract_errors(
        "housekeeping", output_for(orphan), plan, plan_digest, identity
    ) == [
        "handoff_step_output_contract_invalid:housekeeping:identity"
    ]
    # Reversed direction (publication commit recorded as the observed head
    # while identity carries the advanced commit) also fails closed.
    assert contracts._step_output_contract_errors(
        "housekeeping", output_for(pub), plan, plan_digest,
        {**identity, "commit_oid": head},
    ) == [
        "handoff_step_output_contract_invalid:housekeeping:identity"
    ]


def _reset_reprove_record():
    completed_at = 1792300546.0
    stability = {
        "status": "completed",
        "plan": {},
        "plan_digest": "d" * 64,
        "receipt": {
            "schema_version": 1,
            "step": "stability_observation",
            "publication_id": "pub-1",
            "completed_at": completed_at,
            "plan_digest": "d" * 64,
            "output": {
                "plan_digest": "d" * 64,
                "publication_id": "pub-1",
                "continuity_id": "f" * 32,
                "count": 1,
                "target": 10,
                "complete": False,
            },
        },
    }
    record = {
        "identity": {"publication_id": "pub-1", "version": 566},
        "steps": {"stability_observation": stability},
    }
    return record, completed_at


def _reset_projection(*, at, previous_last_version=566, previous_count=1):
    return {
        "continuity_id": "2" * 32,
        "count": 0,
        "target": 10,
        "complete": False,
        "observations": [],
        "reset_history": [
            {
                "at": at,
                "reason": "runtime_process_start",
                "details": {
                    "identity_mismatches": ["repository_head", "process_pid"],
                    "previous_count": previous_count,
                    "previous_last_version": previous_last_version,
                },
            }
        ],
    }


def test_stability_reproof_tolerates_documented_reset(handoff_env, monkeypatch):
    # Twelfth convergence defect (2026-10-18): the stability observation row
    # is process-scoped BY DESIGN; a runtime restart between the receipt and
    # this reproof resets it to zero, so exact live-vs-receipt grading wedges
    # the handoff forever (v566: receipt count=1 at 13:22, restart 14:31
    # reset with previous_count=1/previous_last_version=566).  The reset
    # history entry recorded AFTER the receipt, binding the pre-reset row
    # this receipt observed, is the durable proof.
    import post_publication_handoff_step_contracts as contracts
    import stability_observation

    record, completed_at = _reset_reprove_record()

    # Reset after the receipt carrying the observed pre-reset row: accepted.
    monkeypatch.setattr(
        stability_observation,
        "stability_observation_projection",
        lambda: _reset_projection(at=completed_at + 3600.0),
    )
    assert contracts._reprove_operational_steps(record) == {
        "stability_observation": True,
        "reap_signal": False,
        "priority_eval": False,
    }

    # A reset recorded BEFORE the receipt cannot witness it.
    monkeypatch.setattr(
        stability_observation,
        "stability_observation_projection",
        lambda: _reset_projection(at=completed_at - 60.0),
    )
    with pytest.raises(
        Exception, match="handoff_stability_observation_reproof_mismatch"
    ):
        contracts._reprove_operational_steps(record)

    # A reset whose pre-reset row bound a different version cannot witness
    # this publication's observation.
    monkeypatch.setattr(
        stability_observation,
        "stability_observation_projection",
        lambda: _reset_projection(
            at=completed_at + 3600.0, previous_last_version=999
        ),
    )
    with pytest.raises(
        Exception, match="handoff_stability_observation_reproof_mismatch"
    ):
        contracts._reprove_operational_steps(record)

    # A reset whose pre-reset count is smaller than the receipt's count
    # cannot witness it either.
    monkeypatch.setattr(
        stability_observation,
        "stability_observation_projection",
        lambda: _reset_projection(at=completed_at + 3600.0, previous_count=0),
    )
    with pytest.raises(
        Exception, match="handoff_stability_observation_reproof_mismatch"
    ):
        contracts._reprove_operational_steps(record)
