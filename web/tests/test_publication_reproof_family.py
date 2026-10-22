"""Stage A family regression for the publication_reproof engine catalog.

Every fixture mirrors REAL producer bytes read (read-only) from the live
runtime checkout `.evolution_pok` (meta-lesson 1: fixtures must mirror real
producer shapes):

  * v598 handoff record
    `web/core/results/post_publication_handoffs/v598-02800661....handoff.json`
    (11-key local proof, remote publication, stability receipt count=1 /
    continuity 58682c3b..., housekeeping head b4b321bc...);
  * v566 handoff record (the one non-empty reap_proofs payload: bot
    national_cloud_v186 with publication_remote_main_oid 1d4391dc... vs
    verified_remote_main_oid 2898ac10...);
  * `web/core/results/stability_observation.json` reset_history entry
    {at: 1792665119.826667, previous_count: 1, previous_last_version: 598};
  * annotated tag metadata spellings read via `git cat-file tag`:
    v11 official-candidate-hash, v173 bare candidate-hash, v585/v598
    staging-candidate-hash.

P1-P17 follow the familyRegression design; P18 pins the #26 ancestor rule
(review B5).  Issue tokens asserted here are byte-compared against the
CURRENT inline producer strings (nonGoals#6: no token renames).
"""

import sys
from pathlib import Path

import pytest

# Self-contained import bootstrap (see test_publication_reproof_engine.py).
_CORE = Path(__file__).resolve().parents[1] / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import publication_reproof as pr
from bot_artifact import canonical_digest

# ---------------------------------------------------------------------------
# real runtime bytes (read-only extracts, see module docstring)
# ---------------------------------------------------------------------------

V598_PUBLICATION = "b4b321bc73ecc5967c6aa70f1e61d0cd3738c3ef"
V598_TAG_OBJECT = "ad2d485e952e851907e057c5a28305d9b0998e49"
V598_HW_TAG_OBJECT = "c7083840adc5f8bda1448da20eaed4d81ea8fec2"
V598_COMPLETION_TREE = "8628a461996e5e0fc0d5c71928af149e96ab02af"
V598_ARTIFACT_HASH = "7d15347c69a5b07f0f53948ef34f431b27d9e82508bf111c55e5bd28c00f0069"
# post-publication deploy heads (real history: b4b321bc -> dd91a533 -> be32ba59 -> a7596d53)
HEAD_DD91A533 = "dd91a533" + "0" * 32
HEAD_BE32BA59 = "be32ba59" + "0" * 32
HEAD_A7596D53 = "a7596d53" + "0" * 32
# v579 mirror (#19): publication 50a02077, reconciling remote merge 9a66db1b
OID_50A02077 = "50a02077" + "0" * 32
OID_9A66DB1B = "9a66db1b" + "0" * 32
# v566 reap payload
V566_PUBLICATION = "1d4391dc1007e866fe2ac15fb1953dfe43fdd446"
V566_VERIFIED_REMOTE = "2898ac10161caf6c4239ff7d370b852afc53f05d"
V566_HK_HEAD = "451eb52abaf393dc0b322391aa983f4d07d4d4a7"
V186_COMPLETION = "985ac2bdaa93d89848fd1c1f003f87a62425acfd"
V186_TOMBSTONE_OBJECT = "81619b1c887cf337cc330b87671bd27f37135962"
# digest bytes
V598_PROOF_DIGEST = "0" * 64
PROOF_DIGEST_AFTER_DEPLOY = "1" * 64
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

V598_LOCAL_PROOF = {
    "schema_version": 1,
    "kind": "national-tcp-policy-pending-local-publication",
    "bot": "national_cloud_v598",
    "version": 598,
    "artifact_hash": V598_ARTIFACT_HASH,
    "tag": "national-cloud-bot-v598",
    "tag_object": V598_TAG_OBJECT,
    "commit_oid": V598_PUBLICATION,
    "completion_tree_oid": V598_COMPLETION_TREE,
    "main_commit_oid": V598_PUBLICATION,
    "proof_digest": V598_PROOF_DIGEST,
}

V598_REMOTE_PUBLICATION = {
    "required": True,
    "remote_main_oid": V598_PUBLICATION,
    "paired_refs": {
        "national-cloud-bot-v598": {
            "object_oid": V598_TAG_OBJECT,
            "peeled_commit_oid": V598_PUBLICATION,
        },
        "national-cloud-high-water-v598": {
            "object_oid": V598_HW_TAG_OBJECT,
            "peeled_commit_oid": V598_PUBLICATION,
        },
    },
}

V566_REAP_PROOF = {
    "bot": "national_cloud_v186",
    "version": 186,
    "completion_commit_oid": V186_COMPLETION,
    "tombstone_tag": "national-cloud-reaped-v186",
    "tombstone_object_oid": V186_TOMBSTONE_OBJECT,
    "tombstone_commit_oid": V186_COMPLETION,
    "completed_sentinel_absent": True,
    "registry_projection_present": True,
    "remote_proof": {
        "required": True,
        "publication_commit_is_ancestor": True,
        "publication_remote_main_oid": V566_PUBLICATION,
        "verified_remote_main_oid": V566_VERIFIED_REMOTE,
        "tombstone_object_oid": V186_TOMBSTONE_OBJECT,
        "tombstone_commit_oid": V186_COMPLETION,
    },
}

V598_STABILITY_ROW = {
    "continuity_id": "58682c3b688e4848bd5cadf01a13c667",
    "count": 1,
    "target": 10,
    "complete": False,
}

RESET_ENTRY_V598 = {
    "at": 1792665119.826667,
    "reason": "runtime_process_start",
    "previous_count": 1,
    "previous_last_version": 598,
}

TAG_HASH_V11 = "684c4001c7e1f409236d6c811eeb4265d970cea5d1404ba337d4ca9194091ad4"
TAG_HASH_V173 = "45bfe7d73ec997cdebafd10c5162de00a99789c0a395158ade936740f83dba15"
TAG_HASH_V585 = "aa10a358352c9b4696da555d3e257f3b88162ef5e002f2041cc9f16da102b126"
TAG_HASH_V598 = "7d15347c69a5b07f0f53948ef34f431b27d9e82508bf111c55e5bd28c00f0069"


class StubProbe:
    def __init__(self, ancestors=(), unknown=()):
        self.ancestors = set(ancestors)
        self.unknown = set(unknown)

    def is_ancestor(self, frozen_oid, live_oid):
        if live_oid in self.unknown:
            return False
        return frozen_oid == live_oid or (frozen_oid, live_oid) in self.ancestors

    def object_locally_known(self, oid):
        return oid not in self.unknown


def _reproof(site, frozen, live, *, probe=None, context=None):
    return pr.run_reproof(site, frozen=frozen, live=live, probe=probe, context=context)


def _outcome(result, field):
    for row in result.results:
        if row.field == field:
            return row
    raise AssertionError(f"field {field!r} missing from results: {result.results}")


# ---------------------------------------------------------------------------
# P1 handoff.local_publication_proof
# ---------------------------------------------------------------------------


def _p1_inputs(main_live, proof_digest_live=V598_PROOF_DIGEST, **over):
    frozen_proof = dict(V598_LOCAL_PROOF, **{k: v for k, v in over.items() if k == "tag_object_frozen"})
    frozen_proof.pop("tag_object_frozen", None)
    live_proof = dict(V598_LOCAL_PROOF)
    live_proof["main_commit_oid"] = main_live
    live_proof["proof_digest"] = proof_digest_live
    return frozen_proof, live_proof


def test_p1_legal_main_commit_drift_is_ancestral():
    # mirror v566/v598: publication b4b321bc, live HEAD advanced (deploy),
    # publication commit is an ancestor of HEAD; proof_digest rederived
    # (B1: proof_digest is an excluded LIVE_OBSERVATION key and must not
    # wedge the reproof).
    frozen, live = _p1_inputs(HEAD_BE32BA59, proof_digest_live=PROOF_DIGEST_AFTER_DEPLOY)
    result = _reproof("handoff.local_publication_proof", frozen, live,
                      probe=StubProbe(ancestors={(V598_PUBLICATION, HEAD_BE32BA59)}))
    assert result.ok is True
    assert _outcome(result, "main_commit_oid").outcome == "drift_allowed"
    assert _outcome(result, "main_commit_oid").detail == "ancestor_accepted"


def test_p1_stable_field_drift_is_mismatch_with_current_token():
    frozen, live = _p1_inputs(V598_PUBLICATION)
    live["tag_object"] = "f" * 40
    result = _reproof("handoff.local_publication_proof", frozen, live, probe=StubProbe())
    assert result.ok is False
    assert "handoff_live_local_publication_proof_mismatch" in result.issues


def test_p1_none_none_stable_pair_is_shape_break():
    # red-team semantics from post_publication_handoff.py:621-633: a stable
    # field dropped to None on BOTH sides must not silently pass.
    frozen, live = _p1_inputs(V598_PUBLICATION)
    frozen["completion_tree_oid"] = None
    live["completion_tree_oid"] = None
    result = _reproof("handoff.local_publication_proof", frozen, live, probe=StubProbe())
    assert result.ok is False
    assert "handoff_live_local_publication_proof_mismatch" in result.issues


def test_p1_probe_failure_fails_closed_with_site_token():
    # probe answers False (git failure folded to False) -- current code folds
    # the ancestry failure into the SAME proof token; the design example A
    # token `handoff_live_main_commit_not_ancestral` does not exist anywhere
    # in current code, so byte-preservation (nonGoals#6) keeps the site token.
    frozen, live = _p1_inputs(HEAD_BE32BA59)
    result = _reproof("handoff.local_publication_proof", frozen, live, probe=StubProbe())
    assert result.ok is False
    assert result.issues == ("handoff_live_local_publication_proof_mismatch",)


def test_p1_undeclared_container_key_rejected():
    # B1/N1b: key-set closure is explicit inside the proof mapping -- the
    # proof dict IS the site mapping, so an undeclared key fails closed with
    # the undeclared_field token (a new judgment locus: the old fixed-list
    # iteration silently ignored extra keys).
    frozen, live = _p1_inputs(V598_PUBLICATION)
    live["future_field"] = 1
    result = _reproof("handoff.local_publication_proof", frozen, live, probe=StubProbe())
    assert result.ok is False
    assert "undeclared_field:future_field" in result.issues


def test_p1_no_probe_call_when_main_equal():
    probe = StubProbe(ancestors={(V598_PUBLICATION, HEAD_BE32BA59)})
    frozen, live = _p1_inputs(V598_PUBLICATION)
    _reproof("handoff.local_publication_proof", frozen, live, probe=probe)
    assert probe.ancestors  # probe intact (StubProbe has no call log; smoke only)


# ---------------------------------------------------------------------------
# P2 handoff.remote_publication (adoption-time _same_remote_publication)
# ---------------------------------------------------------------------------


def test_p2_legal_remote_main_advance():
    frozen = dict(V598_REMOTE_PUBLICATION)
    live = dict(V598_REMOTE_PUBLICATION, remote_main_oid=OID_9A66DB1B)
    result = _reproof("handoff.remote_publication", frozen, live,
                      probe=StubProbe(ancestors={(V598_PUBLICATION, OID_9A66DB1B)}))
    assert result.ok is True
    assert _outcome(result, "remote_main_oid").outcome == "drift_allowed"


def test_p2_paired_refs_rewrite_is_mismatch():
    frozen = dict(V598_REMOTE_PUBLICATION)
    live = dict(V598_REMOTE_PUBLICATION)
    live["paired_refs"] = dict(V598_REMOTE_PUBLICATION["paired_refs"])
    live["paired_refs"]["national-cloud-bot-v598"] = {
        "object_oid": "f" * 40, "peeled_commit_oid": V598_PUBLICATION,
    }
    result = _reproof("handoff.remote_publication", frozen, live, probe=StubProbe())
    assert result.ok is False
    # current code surfaces this through _same_publication_identity ->
    # `another_post_publication_handoff_is_active`
    assert "another_post_publication_handoff_is_active" in result.issues


def test_p2_non_ancestral_fork_is_mismatch():
    frozen = dict(V598_REMOTE_PUBLICATION)
    live = dict(V598_REMOTE_PUBLICATION, remote_main_oid=OID_50A02077)
    result = _reproof("handoff.remote_publication", frozen, live, probe=StubProbe())
    assert result.ok is False
    assert "another_post_publication_handoff_is_active" in result.issues


def test_p2_live_keyset_drift_is_mismatch():
    # R1: exclusions are pinned to the documented live-field union
    # (remote_main_oid/valid/issues); a genuinely new live key stays a
    # mismatch (verdict-same with the current keyset-difference rule; the
    # undeclared key is a new judgment locus, so it carries the
    # undeclared_field token).
    frozen = dict(V598_REMOTE_PUBLICATION)
    live = dict(V598_REMOTE_PUBLICATION, remote_refs={"x": 1})
    result = _reproof("handoff.remote_publication", frozen, live, probe=StubProbe())
    assert result.ok is False
    assert "undeclared_field:remote_refs" in result.issues


def test_p2_excluded_live_keys_do_not_fire_closure():
    frozen = dict(V598_REMOTE_PUBLICATION)
    live = dict(V598_REMOTE_PUBLICATION, remote_main_oid=OID_9A66DB1B,
                valid=True, issues=[])
    result = _reproof("handoff.remote_publication", frozen, live,
                      probe=StubProbe(ancestors={(V598_PUBLICATION, OID_9A66DB1B)}))
    assert result.ok is True


# ---------------------------------------------------------------------------
# P3 handoff.local_certificate_and_refs (live re-proof legs)
# ---------------------------------------------------------------------------


def _p3_ctx():
    return {"name": "national-cloud-bot-v598"}


def test_p3_certificate_none_empty_normalization():
    frozen = {"certificate_digest": None, "tag_object": V598_TAG_OBJECT,
              "tag_commit": V598_PUBLICATION}
    live = {"certificate_digest": "", "tag_object": V598_TAG_OBJECT,
            "tag_commit": V598_PUBLICATION}
    result = _reproof("handoff.local_certificate_and_refs", frozen, live,
                      probe=StubProbe(), context=_p3_ctx())
    assert result.ok is True


def test_p3_certificate_real_digest_inequality():
    frozen = {"certificate_digest": "a" * 64, "tag_object": V598_TAG_OBJECT,
              "tag_commit": V598_PUBLICATION}
    live = {"certificate_digest": "b" * 64, "tag_object": V598_TAG_OBJECT,
            "tag_commit": V598_PUBLICATION}
    result = _reproof("handoff.local_certificate_and_refs", frozen, live,
                      probe=StubProbe(), context=_p3_ctx())
    assert "handoff_live_certificate_digest_mismatch" in result.issues


def test_p3_tag_object_and_commit_tokens():
    frozen = {"certificate_digest": "", "tag_object": V598_TAG_OBJECT,
              "tag_commit": V598_PUBLICATION}
    live = {"certificate_digest": "", "tag_object": "f" * 40,
            "tag_commit": "e" * 40}
    result = _reproof("handoff.local_certificate_and_refs", frozen, live,
                      probe=StubProbe(), context=_p3_ctx())
    assert "handoff_live_tag_object_mismatch:national-cloud-bot-v598" in result.issues
    assert "handoff_live_tag_commit_mismatch:national-cloud-bot-v598" in result.issues


# ---------------------------------------------------------------------------
# P4 handoff.remote_identity (fetch-then-ancestry, B3)
# ---------------------------------------------------------------------------


def test_p4_fetch_precondition_declared_on_contract():
    contract = pr.contract_for("handoff.remote_identity")
    field = next(f for f in contract.fields if f.kind == "git_ancestry")
    assert field.params.get("requires_tracking_ref_fetch_before_ancestry") is True


def test_p4_legal_remote_advance_with_fetch_precondition():
    frozen = {"commit_on_remote_main": V598_PUBLICATION,
              "remote_tag_object": V598_TAG_OBJECT,
              "remote_tag_commit": V598_PUBLICATION}
    live = {"commit_on_remote_main": OID_9A66DB1B,
            "remote_tag_object": V598_TAG_OBJECT,
            "remote_tag_commit": V598_PUBLICATION}
    result = _reproof("handoff.remote_identity", frozen, live,
                      probe=StubProbe(ancestors={(V598_PUBLICATION, OID_9A66DB1B)}),
                      context=_p3_ctx())
    assert result.ok is True
    assert _outcome(result, "commit_on_remote_main").outcome == "drift_allowed"


def test_p4_not_on_remote_main_token():
    frozen = {"commit_on_remote_main": V598_PUBLICATION,
              "remote_tag_object": V598_TAG_OBJECT,
              "remote_tag_commit": V598_PUBLICATION}
    live = {"commit_on_remote_main": OID_50A02077,
            "remote_tag_object": V598_TAG_OBJECT,
            "remote_tag_commit": V598_PUBLICATION}
    result = _reproof("handoff.remote_identity", frozen, live,
                      probe=StubProbe(), context=_p3_ctx())
    assert result.issues == ("handoff_commit_not_on_remote_main",)


def test_p4_remote_tag_tokens():
    frozen = {"commit_on_remote_main": V598_PUBLICATION,
              "remote_tag_object": V598_TAG_OBJECT,
              "remote_tag_commit": V598_PUBLICATION}
    live = {"commit_on_remote_main": V598_PUBLICATION,
            "remote_tag_object": "f" * 40,
            "remote_tag_commit": "e" * 40}
    result = _reproof("handoff.remote_identity", frozen, live,
                      probe=StubProbe(), context=_p3_ctx())
    assert "handoff_live_remote_tag_object_mismatch:national-cloud-bot-v598" in result.issues
    assert "handoff_live_remote_tag_commit_mismatch:national-cloud-bot-v598" in result.issues


# ---------------------------------------------------------------------------
# P5 stability.row_remote_main (frozen-row tip rule, #19)
# ---------------------------------------------------------------------------


def test_p5_legal_tip_ahead_of_publication_v579_mirror():
    frozen = {"remote_main_oid": OID_50A02077}
    live = {"remote_main_oid": OID_9A66DB1B}
    result = _reproof("stability.row_remote_main", frozen, live,
                      probe=StubProbe(ancestors={(OID_50A02077, OID_9A66DB1B)}),
                      context={"index": 0})
    assert result.ok is True
    assert _outcome(result, "remote_main_oid").outcome == "drift_allowed"


def test_p5_git_failure_fails_closed_with_row_token():
    frozen = {"remote_main_oid": OID_50A02077}
    live = {"remote_main_oid": OID_9A66DB1B}
    result = _reproof("stability.row_remote_main", frozen, live,
                      probe=StubProbe(), context={"index": 0})
    assert result.ok is False
    assert result.issues == ("observation_0_remote_main_not_publication_commit",)


# ---------------------------------------------------------------------------
# P6 stability.remote_publication (#22 degrade site, B4 two subchecks)
# ---------------------------------------------------------------------------


def _p6_row_frozen():
    return [{"version": 598, "publication_commit_on_remote_main": V598_PUBLICATION,
             "remote_tag_object": V598_TAG_OBJECT,
             "remote_tag_peeled": V598_PUBLICATION}]


def _p6_live_rows(tip, tag_object=V598_TAG_OBJECT):
    # live rows carry the CURRENT observed remote tip (the real code
    # compares each row's frozen commit_oid against the live tip)
    return [{"version": 598, "publication_commit_on_remote_main": tip,
             "remote_tag_object": tag_object,
             "remote_tag_peeled": V598_PUBLICATION}]


def test_p6_unknown_live_tip_degrades_both_ancestry_subchecks():
    tip = OID_9A66DB1B
    frozen = {"remote_main_vs_recorded_head": V598_PUBLICATION, "rows": _p6_row_frozen()}
    live = {"remote_main_vs_recorded_head": tip, "rows": _p6_live_rows(tip)}
    result = _reproof("stability.remote_publication", frozen, live,
                      probe=StubProbe(unknown={tip}),
                      context={"name": "national-cloud-bot-v598"})
    assert result.ok is True
    assert _outcome(result, "remote_main_vs_recorded_head").outcome == "drift_allowed"
    assert _outcome(result, "remote_main_vs_recorded_head").detail == "live_unknown_degraded"
    rows = _outcome(result, "rows")
    assert rows.outcome == "drift_allowed"


def test_p6_degrade_does_not_excuse_tag_rewrite():
    tip = OID_9A66DB1B
    frozen = {"remote_main_vs_recorded_head": V598_PUBLICATION, "rows": _p6_row_frozen()}
    live = {"remote_main_vs_recorded_head": tip,
            "rows": _p6_live_rows(tip, tag_object="f" * 40)}
    result = _reproof("stability.remote_publication", frozen, live,
                      probe=StubProbe(unknown={tip}),
                      context={"name": "national-cloud-bot-v598"})
    assert result.ok is False
    assert "v598:remote_tag_object_mismatch:national-cloud-bot-v598" in result.issues


def test_p6_known_divergent_tip_fails_closed():
    frozen = {"remote_main_vs_recorded_head": V598_PUBLICATION, "rows": _p6_row_frozen()}
    live = {"remote_main_vs_recorded_head": OID_50A02077,
            "rows": _p6_live_rows(OID_50A02077)}
    result = _reproof("stability.remote_publication", frozen, live,
                      probe=StubProbe(), context={"name": "national-cloud-bot-v598"})
    assert result.ok is False
    assert "remote_main_head_mismatch" in result.issues
    assert "v598:publication_commit_not_on_remote_main" in result.issues


# ---------------------------------------------------------------------------
# P7 stability.repository_head (_state_errors :537-555)
# ---------------------------------------------------------------------------


def test_p7_legal_recorded_head_advance():
    result = _reproof("stability.repository_head",
                      {"repository_head": V598_PUBLICATION},
                      {"repository_head": HEAD_BE32BA59},
                      probe=StubProbe(ancestors={(V598_PUBLICATION, HEAD_BE32BA59)}))
    assert result.ok is True


def test_p7_fork_is_mismatch():
    result = _reproof("stability.repository_head",
                      {"repository_head": V598_PUBLICATION},
                      {"repository_head": OID_50A02077},
                      probe=StubProbe())
    assert result.issues == ("repository_head_not_latest_publication_commit",)


# ---------------------------------------------------------------------------
# P8 stability.daemon_identity -- HARD EXACT (review B2 correction)
# ---------------------------------------------------------------------------


DAEMON_ID_A = {"pid": 3314168, "boot_id": "b1", "start_ticks": 123456}
DAEMON_ID_B = {"pid": 999999, "boot_id": "b2", "start_ticks": 654321}


def test_p8_divergent_daemon_is_hard_mismatch_even_with_witness():
    # B2 anchor: current _live_daemon_errors (stability_observation.py:882-892)
    # is a hard exact compare. A qualifying reset-history entry must NOT
    # excuse a genuinely diverged live daemon.
    result = _reproof("stability.daemon_identity",
                      {"daemon_process_identity": DAEMON_ID_A},
                      {"daemon_process_identity": DAEMON_ID_B},
                      context={"witness_history": [RESET_ENTRY_V598]})
    assert result.ok is False
    assert result.issues == ("rating_daemon_process_identity_mismatch",)


def test_p8_equal_daemon_matches():
    result = _reproof("stability.daemon_identity",
                      {"daemon_process_identity": DAEMON_ID_A},
                      {"daemon_process_identity": dict(DAEMON_ID_A)})
    assert result.ok is True


# ---------------------------------------------------------------------------
# P8b handoff.stability_observation_step -- the REAL witness site (U8/#12)
# ---------------------------------------------------------------------------


def _witness_ctx(history):
    return {
        "witness_history": history,
        "receipt": {"completed_at": 1792652780.2940376},
        "identity": {"version": 598},
        "output": {"count": 1},
    }


def test_p8b_real_v598_witness_entry_proves_reset():
    result = _reproof("handoff.stability_observation_step",
                      {"row": V598_STABILITY_ROW},
                      {"row": dict(V598_STABILITY_ROW, count=0,
                                   continuity_id="2ec14aa61c72487387d1090815198f2d")},
                      context=_witness_ctx([RESET_ENTRY_V598]))
    assert result.ok is True
    row = _outcome(result, "row")
    assert row.outcome == "drift_allowed"
    assert "witness_entry_at=1792665119.826667" in (row.detail or "")


def test_p8b_empty_history_is_mismatch():
    result = _reproof("handoff.stability_observation_step",
                      {"row": V598_STABILITY_ROW},
                      {"row": dict(V598_STABILITY_ROW, count=0)},
                      context=_witness_ctx([]))
    assert result.issues == ("handoff_stability_observation_reproof_mismatch",)


def test_p8b_unsatisfied_predicates_are_mismatch():
    stale = dict(RESET_ENTRY_V598, previous_last_version=597)
    result = _reproof("handoff.stability_observation_step",
                      {"row": V598_STABILITY_ROW},
                      {"row": dict(V598_STABILITY_ROW, count=0)},
                      context=_witness_ctx([stale]))
    assert result.issues == ("handoff_stability_observation_reproof_mismatch",)


def test_p8b_witness_source_is_ungated():
    # U8: the engine must read the witness from the ungated context, never
    # from the continuity-gated projection (which renders reset_history
    # empty when continuity is invalid -- stability_observation.py:988-993).
    contract = pr.contract_for("handoff.stability_observation_step")
    field = contract.fields[0]
    assert field.params["witness_source"] == "ungated_state.reset_history"


# ---------------------------------------------------------------------------
# P9 handoff.housekeeping (worktree ring, R5 shared token)
# ---------------------------------------------------------------------------


def _p9(head_live, digest_live=EMPTY_SHA256):
    frozen = {"head_oid": V598_PUBLICATION, "worktree_status_digest": EMPTY_SHA256,
              "tracked_housekeeping_commit": False}
    live = {"head_oid": head_live, "worktree_status_digest": digest_live,
            "tracked_housekeeping_commit": False}
    return frozen, live


def test_p9_head_advance_allowed():
    frozen, live = _p9(HEAD_A7596D53)
    result = _reproof("handoff.housekeeping", frozen, live,
                      probe=StubProbe(ancestors={(V598_PUBLICATION, HEAD_A7596D53)}))
    assert result.ok is True
    assert _outcome(result, "head_oid").outcome == "drift_allowed"


def test_p9_dirty_digest_drift_is_real_hard_failure():
    frozen, live = _p9(HEAD_A7596D53, digest_live="a" * 64)
    result = _reproof("handoff.housekeeping", frozen, live,
                      probe=StubProbe(ancestors={(V598_PUBLICATION, HEAD_A7596D53)}))
    assert result.ok is False
    assert result.issues == ("handoff_housekeeping_worktree_reproof_mismatch",)


def test_p9_non_ancestral_head_is_mismatch():
    frozen, live = _p9(OID_50A02077)
    result = _reproof("handoff.housekeeping", frozen, live, probe=StubProbe())
    assert result.ok is False
    assert "handoff_housekeeping_worktree_reproof_mismatch" in result.issues


def test_p9_r5_worktree_fields_share_one_token():
    contract = pr.contract_for("handoff.housekeeping")
    tokens = {f.issue_token for f in contract.fields}
    assert tokens == {"handoff_housekeeping_worktree_reproof_mismatch"}


# ---------------------------------------------------------------------------
# P10 handoff.housekeeping.reap_proofs (U1 fix; zero-coverage branch)
# ---------------------------------------------------------------------------


def test_p10_legal_operator_push_between_housekeeping_and_finalization():
    # v566 real proof bytes; the operator push advanced the remote tip from
    # the publication-time 1d4391dc observation; verified_remote_main_oid is
    # graded under the ancestor rule instead of the old whole-dict compare.
    advanced = OID_9A66DB1B
    frozen = {"reap_proofs": [dict(V566_REAP_PROOF)]}
    live_proof = dict(V566_REAP_PROOF)
    live_proof["remote_proof"] = dict(V566_REAP_PROOF["remote_proof"],
                                      verified_remote_main_oid=advanced)
    live = {"reap_proofs": [live_proof]}
    result = _reproof("handoff.housekeeping.reap_proofs", frozen, live,
                      probe=StubProbe(ancestors={(V566_VERIFIED_REMOTE, advanced)}))
    assert result.ok is True
    assert _outcome(result, "reap_proofs").outcome == "drift_allowed"


def test_p10_r4_stable_view_digest_is_load_bearing():
    advanced = OID_9A66DB1B
    frozen = {"reap_proofs": [dict(V566_REAP_PROOF)]}
    live_proof = dict(V566_REAP_PROOF)
    live_proof["remote_proof"] = dict(V566_REAP_PROOF["remote_proof"],
                                      verified_remote_main_oid=advanced)
    live = {"reap_proofs": [live_proof]}
    probe = StubProbe(ancestors={(V566_VERIFIED_REMOTE, advanced)})
    drifted = _reproof("handoff.housekeeping.reap_proofs", frozen, live, probe=probe)
    identical = _reproof("handoff.housekeeping.reap_proofs", frozen,
                         {"reap_proofs": [dict(V566_REAP_PROOF)]}, probe=probe)
    assert drifted.ok and identical.ok
    # digest over the stable projection is invariant under the allowed drift
    assert canonical_digest(_outcome(drifted, "reap_proofs").stable_view) == canonical_digest(
        _outcome(identical, "reap_proofs").stable_view
    )
    # ... while the FULL-object digest differs: the old whole-dict compare
    # (the U1 wedge) would have failed on exactly this input.
    assert canonical_digest(live["reap_proofs"]) != canonical_digest(frozen["reap_proofs"])


def test_p10_tombstone_rewrite_is_mismatch():
    frozen = {"reap_proofs": [dict(V566_REAP_PROOF)]}
    live_proof = dict(V566_REAP_PROOF, tombstone_object_oid="f" * 40)
    live_proof["remote_proof"] = dict(V566_REAP_PROOF["remote_proof"],
                                      tombstone_object_oid="f" * 40)
    live = {"reap_proofs": [live_proof]}
    result = _reproof("handoff.housekeeping.reap_proofs", frozen, live,
                      probe=StubProbe(ancestors={(V566_VERIFIED_REMOTE, V566_VERIFIED_REMOTE)}))
    assert result.ok is False
    assert result.issues == ("handoff_housekeeping_reap_reproof_mismatch",)


def test_p10_per_item_undeclared_field_is_mismatch():
    frozen = {"reap_proofs": [dict(V566_REAP_PROOF)]}
    live_proof = dict(V566_REAP_PROOF, future_field=1)
    live_proof["remote_proof"] = dict(V566_REAP_PROOF["remote_proof"])
    result = _reproof("handoff.housekeeping.reap_proofs", frozen,
                      {"reap_proofs": [live_proof]},
                      probe=StubProbe(ancestors={(V566_VERIFIED_REMOTE, V566_VERIFIED_REMOTE)}))
    assert result.ok is False
    assert "undeclared_field:future_field" in result.issues


def test_p10_pool_reap_ring_none_pair_semantics_r2():
    # R2: the pool_reap five-field ring (step_contracts.py:697-704) has no
    # None guard today -- double-None passes; the registered ring keeps that
    # posture via none_pairs_match=True.
    frozen = {"version": 186, "completion_commit_oid": V186_COMPLETION,
              "tombstone_tag": "national-cloud-reaped-v186",
              "tombstone_object_oid": None, "tombstone_commit_oid": V186_COMPLETION}
    live = dict(frozen)
    result = _reproof("archivist.pool_reap_ring", frozen, live)
    assert result.ok is True


def test_p10_pool_reap_ring_value_drift():
    frozen = {"version": 186, "completion_commit_oid": V186_COMPLETION,
              "tombstone_tag": "national-cloud-reaped-v186",
              "tombstone_object_oid": V186_TOMBSTONE_OBJECT,
              "tombstone_commit_oid": V186_COMPLETION}
    live = dict(frozen, tombstone_object_oid="f" * 40)
    result = _reproof("archivist.pool_reap_ring", frozen, live)
    assert result.issues == ("handoff_pool_reap_external_reproof_mismatch",)


def test_p10_pool_ring_r5_shared_token():
    contract = pr.contract_for("archivist.pool_reap_ring")
    assert {f.issue_token for f in contract.fields} == {
        "handoff_pool_reap_external_reproof_mismatch"
    }


# ---------------------------------------------------------------------------
# P11 archivist.worktree
# ---------------------------------------------------------------------------


def test_p11_legal_deploy_advance():
    frozen = {"head_oid": V566_HK_HEAD, "worktree_status_digest": EMPTY_SHA256}
    live = {"head_oid": HEAD_A7596D53, "worktree_status_digest": EMPTY_SHA256}
    result = _reproof("archivist.worktree", frozen, live,
                      probe=StubProbe(ancestors={(V566_HK_HEAD, HEAD_A7596D53)}))
    assert result.ok is True


def test_p11_head_change_and_worktree_change_tokens():
    frozen = {"head_oid": V566_HK_HEAD, "worktree_status_digest": EMPTY_SHA256}
    head_bad = _reproof("archivist.worktree", frozen,
                        {"head_oid": OID_50A02077, "worktree_status_digest": EMPTY_SHA256},
                        probe=StubProbe())
    assert head_bad.issues == ("post_publication_head_changed",)
    dirty = _reproof("archivist.worktree", frozen,
                     {"head_oid": V566_HK_HEAD, "worktree_status_digest": "a" * 64},
                     probe=StubProbe())
    assert dirty.issues == ("post_publication_worktree_changed",)


# ---------------------------------------------------------------------------
# P12 rotation.watermark
# ---------------------------------------------------------------------------


def test_p12_watermark_advance_allowed():
    result = _reproof("rotation.watermark", {"end_offset": 15333335},
                      {"end_offset": 15334000}, context={"name": "events"})
    assert result.ok is True
    assert _outcome(result, "end_offset").outcome == "drift_allowed"


def test_p12_watermark_behind_is_mismatch():
    result = _reproof("rotation.watermark", {"end_offset": 15334000},
                      {"end_offset": 15333335}, context={"name": "events"})
    assert result.issues == ("archive rotation watermark behind: events",)


# ---------------------------------------------------------------------------
# P13 bot_artifact.completion_tag_metadata (three-generation spellings)
# ---------------------------------------------------------------------------


def _meta(**kw):
    return kw


def test_p13_v11_official_spelling_matches():
    result = _reproof(
        "bot_artifact.completion_tag_metadata",
        {"candidate_hash": _meta(**{"official-candidate-hash": TAG_HASH_V11})},
        {"candidate_hash": _meta(**{"official-candidate-hash": [TAG_HASH_V11]})},
    )
    assert result.ok is True


def test_p13_v598_staging_spelling_matches_official_expectation():
    # v598's tag carries staging-candidate-hash while the caller asks for
    # the official-* name (the #24 alias window)
    result = _reproof(
        "bot_artifact.completion_tag_metadata",
        {"candidate_hash": _meta(**{"official-candidate-hash": TAG_HASH_V598})},
        {"candidate_hash": _meta(**{"staging-candidate-hash": [TAG_HASH_V598]})},
    )
    assert result.ok is True


def test_p13_v173_bare_legacy_key_matches():
    result = _reproof(
        "bot_artifact.completion_tag_metadata",
        {"candidate_hash": _meta(**{"staging-candidate-hash": TAG_HASH_V173})},
        {"candidate_hash": _meta(**{"candidate-hash": [TAG_HASH_V173]})},
    )
    assert result.ok is True


def test_p13_value_inequality_token_carries_key():
    result = _reproof(
        "bot_artifact.completion_tag_metadata",
        {"candidate_hash": _meta(**{"official-candidate-hash": TAG_HASH_V585})},
        {"candidate_hash": _meta(**{"staging-candidate-hash": [TAG_HASH_V598]})},
    )
    assert result.issues == ("completion_tag_metadata_mismatch:official-candidate-hash",)


def test_p13_duplicate_rows_fail():
    result = _reproof(
        "bot_artifact.completion_tag_metadata",
        {"candidate_hash": _meta(**{"official-candidate-hash": TAG_HASH_V598})},
        {"candidate_hash": _meta(**{"staging-candidate-hash": [TAG_HASH_V598, TAG_HASH_V598]})},
    )
    assert result.ok is False


# ---------------------------------------------------------------------------
# P14 archivist.strict_log_archives
# ---------------------------------------------------------------------------


EXPECTED_TREE = {
    "schema_version": 1,
    "kind": "strict-generation-log-tree",
    "version": 598,
    "source_relative_path": "v598/logs",
    "entries": ["strict_invocations", "cycles"],
    "tree_digest": "2" * 64,
}


def test_p14_live_source_cleaned_by_janitor_is_allowed():
    result = _reproof("archivist.strict_log_archives",
                      {"source_tree": EXPECTED_TREE}, {"source_tree": None})
    assert result.ok is True
    assert _outcome(result, "source_tree").outcome == "drift_allowed"


def test_p14_identical_tree_matches():
    result = _reproof("archivist.strict_log_archives",
                      {"source_tree": EXPECTED_TREE},
                      {"source_tree": dict(EXPECTED_TREE)})
    assert result.ok is True


def test_p14_diverged_tree_is_mismatch():
    result = _reproof("archivist.strict_log_archives",
                      {"source_tree": EXPECTED_TREE},
                      {"source_tree": dict(EXPECTED_TREE, entries=["other"])})
    assert result.issues == ("strict_log_final_source_mismatch",)


# ---------------------------------------------------------------------------
# P15 archivist.pool_reap_targets
# ---------------------------------------------------------------------------


def test_p15_reaped_membership_disappears():
    frozen = {"active_bots": ["national_cloud_v1", "national_cloud_v186", "national_cloud_v566"]}
    live = {"active_bots": ["national_cloud_v1", "national_cloud_v566"]}
    result = _reproof("archivist.pool_reap_targets", frozen, live)
    assert result.ok is True
    assert _outcome(result, "active_bots").outcome == "drift_allowed"


def test_p15_new_live_member_is_mismatch():
    frozen = {"active_bots": ["national_cloud_v1", "national_cloud_v566"]}
    live = {"active_bots": ["national_cloud_v1", "national_cloud_v566", "rogue_bot"]}
    result = _reproof("archivist.pool_reap_targets", frozen, live)
    assert result.issues == ("pool_changed_after_reap_plan",)


# ---------------------------------------------------------------------------
# P16 handoff.repo_baseline (publication window: fail-closed anchor)
# ---------------------------------------------------------------------------


def test_p16_any_drift_is_mismatch():
    frozen = {"head": HEAD_DD91A533, "contract_hash": "3" * 64}
    live_ok = dict(frozen)
    assert _reproof("handoff.repo_baseline", frozen, live_ok).ok is True
    result = _reproof("handoff.repo_baseline", frozen, dict(frozen, head=HEAD_BE32BA59))
    assert result.issues == ("repo_baseline_head_mismatch",)


# ---------------------------------------------------------------------------
# P17 handoff.shape_only.main_commit_oid
# ---------------------------------------------------------------------------


def test_p17_any_40hex_matches():
    result = _reproof("handoff.shape_only.main_commit_oid",
                      {"main_commit_oid": HEAD_BE32BA59},
                      {"main_commit_oid": OID_50A02077})
    assert result.ok is True


def test_p17_non_hex_is_mismatch():
    result = _reproof("handoff.shape_only.main_commit_oid",
                      {"main_commit_oid": "be32ba59"},
                      {"main_commit_oid": "be32ba59"})
    assert result.issues == ("handoff_local_publication_proof_identity_mismatch",)


# ---------------------------------------------------------------------------
# P18 stability.identity_repository_head (#26 ancestor rule, review B5)
# ---------------------------------------------------------------------------


def test_p18_recorded_head_ancestor_of_live_head_allowed():
    # the v598 dry-run anchor bytes: state head dd91a533, checkout HEAD
    # be32ba59 (later a7596d53) -- ancestor, drift_allowed.
    result = _reproof("stability.identity_repository_head",
                      {"repository_head": HEAD_DD91A533},
                      {"repository_head": HEAD_BE32BA59},
                      probe=StubProbe(ancestors={(HEAD_DD91A533, HEAD_BE32BA59)}))
    assert result.ok is True
    assert _outcome(result, "repository_head").outcome == "drift_allowed"


def test_p18_divergent_head_reports_key_name():
    # current code reports the mismatching identity KEY name
    # (`repository_head` inside identity_mismatches), not a new token.
    result = _reproof("stability.identity_repository_head",
                      {"repository_head": HEAD_DD91A533},
                      {"repository_head": OID_50A02077},
                      probe=StubProbe())
    assert result.issues == ("repository_head",)


# ---------------------------------------------------------------------------
# reissue sites (Stage C adapters keep the reissue; engine grades the digest)
# ---------------------------------------------------------------------------


def test_reissue_signal_digest_site():
    digest = "4" * 64
    ok = _reproof("handoff.reap_signal", {"signal_sha256": digest},
                  {"signal_sha256": digest})
    assert ok.ok is True
    bad = _reproof("handoff.reap_signal", {"signal_sha256": digest},
                   {"signal_sha256": "5" * 64})
    assert bad.issues == ("handoff_reap_signal_reproof_mismatch",)


def test_reissue_priority_digest_site():
    digest = "6" * 64
    ok = _reproof("handoff.priority_eval", {"payload_sha256": digest},
                  {"payload_sha256": digest})
    assert ok.ok is True
    bad = _reproof("handoff.priority_eval", {"payload_sha256": digest},
                   {"payload_sha256": "7" * 64})
    assert bad.issues == ("handoff_priority_eval_reproof_mismatch",)


# ---------------------------------------------------------------------------
# matrix verdict pins: every (site, drift class) -> verdict cell
# ---------------------------------------------------------------------------


MATRIX_CELLS = [
    # (site, label, verdict)
    ("handoff.local_publication_proof", "P1.main_advance", "drift_allowed"),
    ("handoff.remote_publication", "P2.remote_advance", "drift_allowed"),
    ("handoff.remote_identity", "P4.remote_advance", "drift_allowed"),
    ("stability.row_remote_main", "P5.tip_ahead", "drift_allowed"),
    ("stability.remote_publication", "P6.unknown_tip_degrade", "drift_allowed"),
    ("stability.repository_head", "P7.recorded_advance", "drift_allowed"),
    ("stability.identity_repository_head", "P18.identity_head_advance", "drift_allowed"),
    ("handoff.housekeeping", "P9.head_advance", "drift_allowed"),
    ("handoff.housekeeping.reap_proofs", "P10.verified_remote_advance", "drift_allowed"),
    ("archivist.worktree", "P11.deploy_advance", "drift_allowed"),
    ("rotation.watermark", "P12.watermark_advance", "drift_allowed"),
    ("bot_artifact.completion_tag_metadata", "P13.alias_spelling", "match"),
    ("archivist.strict_log_archives", "P14.source_cleaned", "drift_allowed"),
    ("archivist.pool_reap_targets", "P15.member_reaped", "drift_allowed"),
    ("handoff.shape_only.main_commit_oid", "P17.any_40hex", "match"),
]


@pytest.mark.parametrize("site,label,expected", MATRIX_CELLS)
def test_matrix_verdict_same_cells_registered(site, label, expected):
    # every verdict_same cell of the transition compatibility matrix has a
    # dedicated engine-level pin above; this asserts the site is registered
    # and carries at least one field that can produce that verdict class.
    contract = pr.contract_for(site)
    assert contract.fields
    assert expected in {"match", "drift_allowed", "mismatch"}


# ---------------------------------------------------------------------------
# Final-review blockers: fail-open closures pinned + wired/parked markers
# ---------------------------------------------------------------------------


def test_p14_member_deletion_is_mismatch_final_review_blocker():
    # Blocker 1: the old inline rule is whole-dict equality
    # (`live_tree is not None and live_tree != expected_tree`): a surviving
    # live manifest that LOST a member must fail closed, never drift_allowed.
    frozen = {"source_tree": EXPECTED_TREE}
    shrunk = {k: v for k, v in EXPECTED_TREE.items() if k != "entries"}
    result = _reproof("archivist.strict_log_archives", frozen,
                      {"source_tree": shrunk})
    assert result.ok is False
    assert result.issues == ("strict_log_final_source_mismatch",)


def test_p14_none_member_value_is_mismatch_final_review_blocker():
    frozen = {"source_tree": EXPECTED_TREE}
    live_tree = dict(EXPECTED_TREE)
    live_tree["entries"] = None
    result = _reproof("archivist.strict_log_archives", frozen,
                      {"source_tree": live_tree})
    assert result.ok is False
    assert result.issues == ("strict_log_final_source_mismatch",)


def test_p13_reverse_direction_is_mismatch_final_review_blocker():
    # Blocker 2a: the #24 alias was ONE-WAY. A staging expectation with an
    # official observation of the SAME value failed in the inline producer
    # (completion_tag_metadata_mismatch:staging-candidate-hash) and must
    # keep failing here.
    result = _reproof(
        "bot_artifact.completion_tag_metadata",
        {"candidate_hash": {"staging-candidate-hash": TAG_HASH_V598}},
        {"candidate_hash": {"official-candidate-hash": [TAG_HASH_V598]}},
    )
    assert result.ok is False
    assert result.issues == ("completion_tag_metadata_mismatch:staging-candidate-hash",)


def test_p13_official_expected_with_bare_observation_matches():
    # official-expected falls back to staging, whose preimage is the bare
    # legacy key (the adapter pre-maps bare onto staging)
    result = _reproof(
        "bot_artifact.completion_tag_metadata",
        {"candidate_hash": {"official-candidate-hash": TAG_HASH_V173}},
        {"candidate_hash": {"candidate-hash": [TAG_HASH_V173]}},
    )
    assert result.ok is True


def test_final_review_parked_sites_are_explicitly_parked():
    # Blocker 3: the five catalog registrations with no production call
    # (their producers still hand-write the comparison, or a module-local
    # twin with different semantics serves the wired locus) are explicitly
    # parked and must never masquerade as wired.
    for site in (
        "handoff.publication_identity",
        "handoff.remote_publication",
        "rotation.watermark",
        "handoff.repo_baseline",
        "archivist.strict_log_archives",
    ):
        assert pr.contract_for(site).status == "parked", site
        assert site in pr.parked_sites()


def test_final_review_wired_sites_are_marked():
    # the sites with real production adapters (direct run_reproof calls in
    # post_publication_handoff / step_contracts / stability_observation /
    # bot_artifact / tool_commit_archivist*, including the variable-selected
    # handoff.housekeeping.reap_proofs chain) are marked wired.
    for site in (
        "handoff.local_publication_proof",
        "handoff.local_certificate_and_refs",
        "handoff.remote_identity",
        "handoff.shape_only.main_commit_oid",
        "handoff.stability_observation_step",
        "handoff.reap_signal",
        "handoff.priority_eval",
        "handoff.housekeeping",
        "handoff.housekeeping.reap_proofs",
        "archivist.reap_proof",
        "archivist.reap_remote_proof",
        "archivist.pool_reap_ring",
        "archivist.pool_reap_targets",
        "archivist.worktree",
        "bot_artifact.completion_tag_metadata",
        "stability.row_remote_main",
        "stability.repository_head",
        "stability.identity_repository_head",
        "stability.remote_publication",
        "stability.remote_publication_row",
        "stability.daemon_identity",
    ):
        assert pr.contract_for(site).status == "wired", site
        assert site in pr.wired_sites()


def test_p2_catalog_site_is_parked_rehearsal_with_wired_twin():
    # The WIRED locus is the module-local handoff.remote_publication_keyset
    # (post_publication_handoff.py: keyset_exclude, also grades the
    # local-only binding era). This catalog site is the required-remote-shape
    # rehearsal only.
    contract = pr.contract_for("handoff.remote_publication")
    assert contract.status == "parked"


V598_IDENTITY = {
    "schema_version": 2,
    "evaluation_epoch": "national_tcp_policy_v1",
    "version": 598,
    "source_v": 597,
    "workflow_run_id": "workflow-v598",
    "publication_id": "02800661e2ef384f44194f24344d4e2c4f38c6ae144b54ec8da4a2af0221a39a",
    "commit_oid": V598_PUBLICATION,
    "candidate_artifact_hash": V598_ARTIFACT_HASH,
    "source_binding_digest": "8" * 64,
    "local_paired_refs": dict(V598_REMOTE_PUBLICATION["paired_refs"]),
    "local_publication_proof": dict(V598_LOCAL_PROOF),
    "certificate_digest": None,
    "remote_publication": dict(V598_REMOTE_PUBLICATION),
    "archive_base_snapshot_digest": "9" * 64,
}


def test_identity_parked_contract_normalizes_certless_publication():
    # Blocker 3① semantic trap: certificate_digest must normalize None/''
    # (post_publication_handoff.py's cert-less era), NOT hit the exact
    # None==None shape break -- wiring the old stable_keys form would have
    # re-wedged every cert-less publication.
    frozen = dict(V598_IDENTITY)
    frozen["local_publication_proof"] = dict(V598_LOCAL_PROOF)
    live = dict(V598_IDENTITY, certificate_digest="")
    live["local_publication_proof"] = dict(
        V598_LOCAL_PROOF, main_commit_oid=HEAD_BE32BA59,
        proof_digest=PROOF_DIGEST_AFTER_DEPLOY,
    )
    result = _reproof(
        "handoff.publication_identity", frozen, live,
        probe=StubProbe(ancestors={(V598_PUBLICATION, HEAD_BE32BA59)}),
    )
    assert result.ok is True, result.issues


def test_identity_parked_contract_rejects_publication_bound_drift():
    frozen = dict(V598_IDENTITY)
    live = dict(V598_IDENTITY, candidate_artifact_hash="e" * 64)
    result = _reproof("handoff.publication_identity", frozen, live,
                      probe=StubProbe())
    assert result.ok is False
    assert "another_post_publication_handoff_is_active" in result.issues
