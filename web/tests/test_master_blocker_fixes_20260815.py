"""Regressions for the two Master-entry blockers that burned v176-v183.

1. `master_generation_evidence_binding_invalid` (checkpoint_snapshot_manifest_
   digest_mismatch): up to max_ahead draft slots each ran a full prepare with
   force=True, rmtree+rebuilding the SAME per-version evidence snapshot (the
   manifest embeds created_at, so every freeze yields a new digest). The
   primary checkpoint's bound digests then mismatched the last draft's freeze
   at Master validation. Fix: only the primary force-freezes
   (``_h2h_freeze_force``); drafts reuse a valid existing snapshot.

2. `master_literature_probe_receipt_invalid` (chronic since v163): a scout
   LLM dispatch error writes an `infra_failure` retry overlay into the
   checkpoint; `_literature_checkpoint_identity` did not strip that key, so
   the valid probe receipt failed `literature_checkpoint_semantic_identity_
   mismatch` and the run_master retry the overlay requested was killed by the
   gate 13s later. Fix: strip `infra_failure` like `audit_attempt`.
"""

import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


def test_h2h_freeze_force_only_primary_lane():
    import generation_scheduler as gs

    # The primary lane (no slot) force-refreshes the frozen cycle.
    assert gs._h2h_freeze_force(None) is True
    # Every draft slot must REUSE a valid existing snapshot instead of
    # rmtree+re-freezing the shared per-version dir under the primary.
    assert gs._h2h_freeze_force("draft") is False
    assert gs._h2h_freeze_force("draft1") is False
    assert gs._h2h_freeze_force("consumer-candidate-v179") is False


def _minimal_probe_checkpoint() -> dict:
    return {
        "schema_version": 2,
        "checkpoint_revision": 4,
        "stage": "direction_audited",
        "next_v": 180,
        "source_v": 83,
        "workflow_run_id": "generation:180:workflow-v1",
        "audit_context": {
            "selection": {"strategy": "s", "current_v": 173},
        },
        "gate_results": {},
    }


def test_literature_identity_ignores_infra_failure_overlay():
    from tool_planning_literature_probe import _literature_checkpoint_identity

    base = _minimal_probe_checkpoint()
    identity_before = _literature_checkpoint_identity(base, origin_revision=4)

    # A scout LLM dispatch error asks for a run_master retry by writing an
    # infra overlay into the checkpoint. That retry bookkeeping must not
    # change the semantic identity, or the retry it requests is killed by
    # literature_checkpoint_semantic_identity_mismatch (v174/v178/v180).
    with_overlay = _minimal_probe_checkpoint()
    with_overlay["infra_failure"] = {
        "failure_class": "infrastructure",
        "component": "master_llm",
        "code": "master_llm_unavailable",
        "owner_tool": "run_master",
        "attempt": 1,
        "max_attempts": 6,
        "exhausted": False,
        "action": "retry_same_tool",
    }
    identity_after = _literature_checkpoint_identity(
        with_overlay, origin_revision=4
    )
    assert identity_before == identity_after


def test_literature_identity_still_binds_semantic_fields():
    from tool_planning_literature_probe import _literature_checkpoint_identity

    base = _minimal_probe_checkpoint()
    a = _literature_checkpoint_identity(base, origin_revision=4)

    # A genuine semantic change (different source parent) MUST change the
    # identity — the strip list must not weaken the binding.
    changed = _minimal_probe_checkpoint()
    changed["source_v"] = 105
    b = _literature_checkpoint_identity(changed, origin_revision=4)
    assert a != b

    # Direction-audit content changes still bind too.
    changed2 = _minimal_probe_checkpoint()
    changed2["audit_context"]["direction_audit"] = {"digest": "x"}
    c = _literature_checkpoint_identity(changed2, origin_revision=4)
    assert a != c


def test_literature_identity_ignores_repo_baseline_head_drift_repair():
    from tool_planning_literature_probe import (
        _literature_checkpoint_identity,
        _literature_checkpoint_identity_legacy,
        _literature_checkpoint_identity_matches,
    )

    base = _minimal_probe_checkpoint()
    base["repo_baseline"] = {"head": "a" * 40, "contract_hash": "x"}
    a = _literature_checkpoint_identity(base, origin_revision=4)

    drifted = _minimal_probe_checkpoint()
    drifted["repo_baseline"] = {"head": "b" * 40, "contract_hash": "y"}
    b = _literature_checkpoint_identity(drifted, origin_revision=4)
    assert a == b

    # Legacy receipts still validate against an *unchanged* checkpoint, so a
    # deploy does not fail-closed a still-valid research binding.
    legacy = _literature_checkpoint_identity_legacy(base, origin_revision=4)
    assert _literature_checkpoint_identity_matches(legacy, base, origin_revision=4)
    # After HEAD-drift repair the legacy hash diverges; the router must
    # re-run the probe instead of treating the old hash as live.
    assert not _literature_checkpoint_identity_matches(
        legacy, drifted, origin_revision=4
    )


def test_literature_receipt_present_replays_probe_after_legacy_head_drift():
    from master_context_contract import build_master_context
    from pipeline_state import (
        literature_probe_receipt_binding,
        literature_probe_receipt_present,
    )
    from tool_planning_literature_probe import (
        _LITERATURE_PROBE_CHECKPOINT_BINDING_SCHEMA,
        _literature_checkpoint_identity,
        _literature_checkpoint_identity_legacy,
    )

    master_context = build_master_context(
        next_v=300,
        source_v=299,
        stagnation_info="STAGNATION_DETECTED (is_stagnant=true)",
    )
    ckpt = {
        "stage": "direction_audited",
        "next_v": 300,
        "source_v": 299,
        "workflow_run_id": "generation:300:workflow-v1",
        "checkpoint_revision": 5,
        "repo_baseline": {"head": "b" * 40},
        "direction_audit": {
            "repetition_detected": True,
            "suggested_direction": "river thin-value leak",
        },
        "audit_context": {"master_context": master_context},
    }
    binding, errors = literature_probe_receipt_binding(ckpt)
    assert not errors

    old = dict(ckpt)
    old["repo_baseline"] = {"head": "a" * 40}
    old["checkpoint_revision"] = 4
    stored_legacy = _literature_checkpoint_identity_legacy(old, origin_revision=4)

    def _receipt(identity: str) -> dict:
        return {
            "next_v": 300,
            "source_v": 299,
            "reason": "completed",
            **binding,
            "producer_receipt": {
                "checkpoint_binding": {
                    "schema": _LITERATURE_PROBE_CHECKPOINT_BINDING_SCHEMA,
                    "checkpoint_identity": identity,
                    "checkpoint_revision": 4,
                    "workflow_run_id": "generation:300:workflow-v1",
                    "stage": "direction_audited",
                    "next_v": 300,
                    "source_v": 299,
                    "requirement_context_digest": binding[
                        "requirement_context_digest"
                    ],
                }
            },
        }

    ckpt["literature_probe"] = _receipt(stored_legacy)
    assert literature_probe_receipt_present(ckpt) is False

    ckpt["literature_probe"] = _receipt(
        _literature_checkpoint_identity(old, origin_revision=4)
    )
    assert literature_probe_receipt_present(ckpt) is True

