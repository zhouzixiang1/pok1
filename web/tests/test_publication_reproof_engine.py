"""Stage A engine unit tests for web/core/publication_reproof.py.

Covers (per the Stage A design + review blockers B1-B6):
  * C1-C7 declaration-time ("compile-time") rejection face,
  * N1/N1b/N1c/N1d undeclared-drift negatives,
  * K1-K11 per-kind verdict pins (match / drift_allowed / mismatch).

The production site catalog registered by the module is exercised in
test_publication_reproof_family.py; this file grades kinds through
throwaway ``test.tmp.*`` sites so every rule is independently testable.
"""

import itertools
import sys
from pathlib import Path

import pytest

# Self-contained import bootstrap: resolve web/core from this file's own
# location so the module imports under ANY cwd, even if the suite conftest
# path setup were absent (belt-and-braces; conftest.py normally provides
# this via PROJECT_ROOT/web-core insertion).
_CORE = Path(__file__).resolve().parents[1] / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import publication_reproof as pr

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_SEQ = itertools.count()


def _tmp_site(fields, **kwargs):
    site_id = f"test.tmp.{next(_SEQ)}"
    pr.register_contract(pr.ReproofContract(site_id=site_id, fields=tuple(fields), **kwargs))
    return site_id


def _grade(fields, frozen, live, *, probe=None, context=None, **kwargs):
    site_id = _tmp_site(fields, **kwargs)
    return pr.run_reproof(site_id, frozen=frozen, live=live, probe=probe, context=context)


class StubProbe:
    """Deterministic GitProbe stub: ancestry from an explicit pair set."""

    def __init__(self, ancestors=(), unknown=()):
        self.ancestors = set(ancestors)
        self.unknown = set(unknown)
        self.calls = []

    def is_ancestor(self, frozen_oid, live_oid):
        self.calls.append(("is_ancestor", frozen_oid, live_oid))
        if live_oid in self.unknown:
            return False
        return frozen_oid == live_oid or (frozen_oid, live_oid) in self.ancestors

    def object_locally_known(self, oid):
        self.calls.append(("object_locally_known", oid))
        return oid not in self.unknown


A = "a" * 40
B = "b" * 40
C = "c" * 40
D64 = "d" * 64
E64 = "e" * 64


# ---------------------------------------------------------------------------
# C: declaration-time rejection face ("compile-time" errors)
# ---------------------------------------------------------------------------


def test_c1_unknown_kind_rejected():
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec("x", "not_a_kind", "container", "tok", {}),),
        ))


def test_c1b_unknown_field_class_rejected():
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec("x", "exact", "not_a_class", "tok", {}),),
        ))


def test_c2_kind_class_mismatch_rejected():
    # exact declared over a git-observed (frozen_observation) field: the
    # original #1-16/#19/#21 defect class, refused at declaration time.
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec("main_commit_oid", "exact", "frozen_observation", "tok", {}),),
        ))
    # and git_ancestry on a container field
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec("box", "git_ancestry", "container", "tok", {}),),
        ))


def test_c3_degrade_outside_site_whitelist_rejected():
    # B4: degrade_when_live_unknown is bound to the stability remote-tip
    # site whitelist, not a generic kind x class rule.
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec(
                "tip", "git_ancestry", "frozen_observation", "tok",
                {"tip_kind": "literal_oid", "hex": 40, "equal_passes": True,
                 "degrade_when_live_unknown": True,
                 "degrade_requires": "site_declares_ls_remote_reachability_first"},
            ),),
        ))


def test_c3b_degrade_requires_literal_tip_and_reason():
    # degrade declared on the whitelisted site but with tip_kind="head"
    # (always locally known) must still be refused.
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id="stability.remote_publication",
            fields=(pr.FieldSpec(
                "remote_main_vs_recorded_head", "git_ancestry", "frozen_observation",
                "remote_main_head_mismatch",
                {"tip_kind": "head", "hex": 40, "equal_passes": True,
                 "degrade_when_live_unknown": True},
            ),),
        ))


def test_c4_spelling_alias_value_transform_rejected():
    # any value-transformer param is a fail-closed boundary relaxation
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec(
                "meta", "spelling_alias", "container", "tok",
                {"alias_groups": {"k": ("k1", "k2")},
                 "value_transformer": lambda v: v.lower()},
            ),),
        ))
    # a non-key-name (callable) inside an alias group is a value transform
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec(
                "meta", "spelling_alias", "container", "tok",
                {"alias_groups": {"k": ("k1", lambda v: v)}},
            ),),
        ))


def test_c5_witness_source_gated_projection_rejected():
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec(
                "count", "witness_reset", "process_scoped", "tok",
                {"witness_source": "projection.reset_history",
                 "predicates": {"at_gte": "receipt.completed_at"},
                 "entry_level_fields": True},
            ),),
        ))


def test_c5b_witness_unknown_predicate_rejected():
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec(
                "count", "witness_reset", "process_scoped", "tok",
                {"witness_source": "ungated_state.reset_history",
                 "predicates": {"made_up_predicate": "receipt.completed_at"},
                 "entry_level_fields": True},
            ),),
        ))


def test_c6_shape_only_without_provenance_rejected():
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec("oid", "shape_only", "frozen_observation", "tok",
                                 {"hex": 40}),),
        ))


def test_c7_duplicate_site_id_rejected():
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.contract_for("handoff.local_publication_proof"))


def test_n2_contract_exclude_key_outside_live_registry_rejected():
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec("x", "exact", "container", "tok", {}),),
            exclude_keys=("not_a_live_observation_key",),
        ))


def test_unknown_param_key_for_kind_rejected():
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec("x", "exact", "container", "tok",
                                 {"surprise_param": 1}),),
        ))


def test_equal_passes_false_rejected():
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec("x", "git_ancestry", "frozen_observation", "tok",
                                 {"tip_kind": "head", "hex": 40, "equal_passes": False}),),
        ))


def test_shared_token_group_inconsistency_rejected():
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(
                pr.FieldSpec("a", "exact", "container", "same_token",
                             {"shared_token_group": "g"}),
                pr.FieldSpec("b", "exact", "container", "different_token",
                             {"shared_token_group": "g"}),
            ),
        ))


def test_fetch_precondition_param_rejected_outside_bound_sites():
    # B3: the tracking-ref fetch precondition is only declarable on the two
    # sites whose current code actually fetches before the ancestry check.
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec(
                "tip", "git_ancestry", "frozen_observation", "tok",
                {"tip_kind": "literal_oid", "hex": 40, "equal_passes": True,
                 "requires_tracking_ref_fetch_before_ancestry": True},
            ),),
        ))


def test_item_contract_unresolved_fails_at_run_time():
    site_id = _tmp_site((pr.FieldSpec(
        "items", "stable_subset", "container", "tok",
        {"stable_keys": ["bot"], "per_item_key": "bot",
         "item_contract": "test.never.registered"},
    ),))
    with pytest.raises(pr.ContractError):
        pr.run_reproof(site_id, frozen={"items": [{"bot": "x"}]},
                       live={"items": [{"bot": "x"}]}, probe=StubProbe())


# ---------------------------------------------------------------------------
# N: undeclared-drift negatives (the structural anchor for the 24 defects)
# ---------------------------------------------------------------------------


def test_n1_top_level_undeclared_frozen_key_is_mismatch():
    result = _grade(
        (pr.FieldSpec("x", "exact", "container", "tok_x", {}),),
        {"x": 1, "future_field": 1},
        {"x": 1},
    )
    assert result.ok is False
    assert "undeclared_field:future_field" in result.issues


def test_n1_top_level_undeclared_live_key_is_mismatch():
    # closed key policy binds BOTH sides at the top level
    result = _grade(
        (pr.FieldSpec("x", "exact", "container", "tok_x", {}),),
        {"x": 1},
        {"x": 1, "future_field": 2},
    )
    assert result.ok is False
    assert "undeclared_field:future_field" in result.issues


def test_n1_declared_exclude_key_is_tolerated():
    result = _grade(
        (pr.FieldSpec("x", "exact", "container", "tok_x", {}),),
        {"x": 1, "valid": True},
        {"x": 1, "valid": False},
        exclude_keys=("valid",),
    )
    assert result.ok is True
    assert result.issues == ()


def test_n1b_container_undeclared_key_is_mismatch_both_sides():
    spec = pr.FieldSpec(
        "box", "stable_subset", "container", "tok_box",
        {"stable_keys": ["a", "b"]},
    )
    frozen_side = _grade((spec,), {"box": {"a": 1, "b": 2, "future": 3}},
                         {"box": {"a": 1, "b": 2}})
    assert frozen_side.ok is False
    assert "tok_box" in frozen_side.issues
    assert any("undeclared_field:future" in (r.detail or "") for r in frozen_side.results)

    live_side = _grade((spec,), {"box": {"a": 1, "b": 2}},
                       {"box": {"a": 1, "b": 2, "future": 3}})
    assert live_side.ok is False


def test_n1c_per_item_undeclared_field_is_mismatch():
    # a per-item sub-contract with closed key policy catches an undeclared
    # field injected inside ONE item (the U1 whole-dict blind spot).
    item_site = _tmp_site((
        pr.FieldSpec("bot", "exact", "container", "tok_item", {}),
        pr.FieldSpec("version", "exact", "container", "tok_item", {}),
    ))
    outer = _tmp_site((pr.FieldSpec(
        "proofs", "stable_subset", "container", "tok_outer",
        {"stable_keys": ["bot", "version"], "per_item_key": "bot",
         "item_contract": item_site},
    ),))
    result = pr.run_reproof(
        outer,
        frozen={"proofs": [{"bot": "b1", "version": 1}]},
        live={"proofs": [{"bot": "b1", "version": 1, "future_field": 9}]},
        probe=StubProbe(),
    )
    assert result.ok is False
    # the child closure violation propagates its own token through the
    # container field result
    assert "undeclared_field:future_field" in result.issues


def test_n1d_keyset_exclude_live_keyset_drift_is_mismatch():
    result = _grade(
        (pr.FieldSpec(
            "box", "keyset_exclude", "container", "tok_ks",
            {"exclude_keys": ["remote_main_oid"]},
        ),),
        {"box": {"required": True, "paired": {}, "remote_main_oid": A}},
        {"box": {"required": True, "paired": {}, "remote_main_oid": B,
                 "brand_new_key": 1}},
    )
    assert result.ok is False
    assert "tok_ks" in result.issues


# ---------------------------------------------------------------------------
# K: per-kind verdict pins
# ---------------------------------------------------------------------------


def test_k1_exact_rules():
    spec = pr.FieldSpec("x", "exact", "container", "tok_x", {})
    assert _grade((spec,), {"x": 5}, {"x": 5}).results[0].outcome == "match"
    bad = _grade((spec,), {"x": 5}, {"x": 6})
    assert bad.results[0].outcome == "mismatch"
    assert bad.issues == ("tok_x",)
    # type-sensitive: 1 != "1"
    assert _grade((spec,), {"x": 1}, {"x": "1"}).results[0].outcome == "mismatch"
    # None == None is a shape break by default (red-team semantics)
    none_pair = _grade((spec,), {"x": None}, {"x": None})
    assert none_pair.results[0].outcome == "mismatch"
    # ... unless the site declares none_pairs_match (R2-era semantics)
    spec_nn = pr.FieldSpec("x", "exact", "container", "tok_x", {"none_pairs_match": True})
    assert _grade((spec_nn,), {"x": None}, {"x": None}).results[0].outcome == "match"


def test_k2_exact_digest_rules():
    spec = pr.FieldSpec("x", "exact_digest", "container", "tok_d", {})
    assert _grade((spec,), {"x": D64}, {"x": D64}).results[0].outcome == "match"
    assert _grade((spec,), {"x": D64}, {"x": E64}).results[0].outcome == "mismatch"
    assert _grade((spec,), {"x": "not-hex"}, {"x": "not-hex"}).results[0].outcome == "mismatch"
    assert _grade((spec,), {"x": D64}, {"x": 42}).results[0].outcome == "mismatch"


def test_k3_spelling_alias_rules():
    spec = pr.FieldSpec(
        "meta", "spelling_alias", "container", "tok_alias:{key}",
        {"alias_groups": {"canonical": ("official-key", "staging-key", "bare-key")},
         "expected_key_fallback": {"official-key": "staging-key"},
         "live_key_preimage": {"staging-key": "bare-key"},
         "value_shape": "single_item_list_str"},
    )
    # same value under different key spellings -> match (live side carries
    # the producer's accumulated single-row list)
    ok = _grade((spec,), {"meta": {"official-key": "V"}},
                {"meta": {"staging-key": ["V"]}})
    assert ok.results[0].outcome == "match"
    # bare legacy spelling carries the same value -> match (resolution order)
    ok_bare = _grade((spec,), {"meta": {"official-key": "V"}},
                     {"meta": {"bare-key": ["V"]}})
    assert ok_bare.results[0].outcome == "match"
    # value inequality -> mismatch, token carries the EXPECTED key name
    # (current producers iterate expected_metadata keys for the token)
    bad = _grade((spec,), {"meta": {"official-key": "V"}},
                 {"meta": {"staging-key": ["W"]}})
    assert bad.results[0].outcome == "mismatch"
    assert bad.issues == ("tok_alias:official-key",)
    # duplicate accumulated rows ([v, v]) fail (single-element list exactness)
    dup = _grade((spec,), {"meta": {"official-key": "V"}},
                 {"meta": {"staging-key": ["V", "V"]}})
    assert dup.results[0].outcome == "mismatch"
    # alias key absent on a side -> mismatch
    absent = _grade((spec,), {"meta": {}}, {"meta": {"staging-key": "V"}})
    assert absent.results[0].outcome == "mismatch"
    # scalar shape with None/'' normalization
    scalar = pr.FieldSpec(
        "cert", "spelling_alias", "container", "tok_cert",
        {"alias_groups": {"certificate_digest": ("certificate_digest",)},
         "none_normalization": True, "value_shape": "scalar"},
    )
    assert _grade((scalar,), {"cert": {"certificate_digest": None}},
                  {"cert": {"certificate_digest": ""}}).results[0].outcome == "match"
    assert _grade((scalar,), {"cert": {"certificate_digest": D64}},
                  {"cert": {"certificate_digest": E64}}).results[0].outcome == "mismatch"


def test_k4_git_ancestry_rules():
    spec = pr.FieldSpec("tip", "git_ancestry", "frozen_observation", "tok_a",
                        {"tip_kind": "head", "hex": 40, "equal_passes": True})
    probe = StubProbe(ancestors={(A, B)})
    # equal -> match, probe never consulted
    eq = _grade((spec,), {"tip": A}, {"tip": A}, probe=probe)
    assert eq.results[0].outcome == "match"
    assert probe.calls == []
    # ancestor -> drift_allowed
    adv = _grade((spec,), {"tip": A}, {"tip": B}, probe=probe)
    assert adv.results[0].outcome == "drift_allowed"
    assert adv.ok is True
    # probe failure folds to False -> mismatch (fail-closed, #19 semantics)
    fail = _grade((spec,), {"tip": A}, {"tip": C}, probe=probe)
    assert fail.results[0].outcome == "mismatch"
    assert fail.issues == ("tok_a",)
    # non-hex live value -> mismatch
    nh = _grade((spec,), {"tip": A}, {"tip": "zz"}, probe=probe)
    assert nh.results[0].outcome == "mismatch"
    # missing values -> mismatch
    assert _grade((spec,), {"tip": None}, {"tip": B}, probe=probe).results[0].outcome == "mismatch"


def test_k5_stable_subset_rules():
    spec = pr.FieldSpec(
        "box", "stable_subset", "container", "tok_s",
        {"stable_keys": ["a", "b"],
         "container_exclude_keys": {"live_k": "verification_time_observation"}},
    )
    assert _grade((spec,), {"box": {"a": 1, "b": 2, "live_k": A}},
                  {"box": {"a": 1, "b": 2, "live_k": B}}).results[0].outcome == "match"
    # stable-key drift -> mismatch
    assert _grade((spec,), {"box": {"a": 1, "b": 2}}, {"box": {"a": 1, "b": 3}}).results[0].outcome == "mismatch"
    # stable key absent on one side (None == None) -> shape break
    assert _grade((spec,), {"box": {"a": 1, "b": None}}, {"box": {"a": 1, "b": None}}).results[0].outcome == "mismatch"
    # nested_contract delegation aggregates drift from an ancestry field
    inner = _tmp_site((pr.FieldSpec("oid", "git_ancestry", "frozen_observation",
                                    "tok_inner", {"tip_kind": "head", "hex": 40,
                                                  "equal_passes": True}),))
    outer = pr.FieldSpec("box", "stable_subset", "container", "tok_s",
                         {"nested_contract": inner})
    adv = _grade((outer,), {"box": {"oid": A}}, {"box": {"oid": B}},
                 probe=StubProbe(ancestors={(A, B)}))
    assert adv.results[0].outcome == "drift_allowed"
    bad = _grade((outer,), {"box": {"oid": A}}, {"box": {"oid": B, "extra": 1}},
                 probe=StubProbe(ancestors={(A, B)}))
    assert bad.results[0].outcome == "mismatch"
    # the nested closure token propagates through the container result
    assert bad.results[0].issue_token == "undeclared_field:extra"


def test_k6_keyset_exclude_rules():
    spec = pr.FieldSpec("box", "keyset_exclude", "container", "tok_k",
                        {"exclude_keys": ["remote_main_oid"]})
    ok = _grade((spec,), {"box": {"required": True, "remote_main_oid": A}},
                {"box": {"required": True, "remote_main_oid": B}})
    assert ok.results[0].outcome == "match"
    # live-only key
    assert _grade((spec,), {"box": {"required": True}},
                  {"box": {"required": True, "extra": 1}}).results[0].outcome == "mismatch"
    # frozen-only key
    assert _grade((spec,), {"box": {"required": True, "gone": 1}},
                  {"box": {"required": True}}).results[0].outcome == "mismatch"
    # common key value drift
    assert _grade((spec,), {"box": {"required": True}},
                  {"box": {"required": False}}).results[0].outcome == "mismatch"
    # None value on a common key fails (old _same_remote_publication semantics)
    assert _grade((spec,), {"box": {"required": None}},
                  {"box": {"required": None}}).results[0].outcome == "mismatch"


def test_k7_witness_reset_rules():
    spec = pr.FieldSpec(
        "count", "witness_reset", "process_scoped", "tok_w",
        {"witness_source": "ungated_state.reset_history",
         "predicates": {"at_gte": "receipt.completed_at",
                        "previous_last_version_eq": "identity.version",
                        "previous_count_gte": "output.count"},
         "entry_level_fields": True},
    )
    ctx = {
        "witness_history": [{"at": 200.0, "reason": "runtime_process_start",
                             "previous_count": 1, "previous_last_version": 598}],
        "receipt": {"completed_at": 100.0},
        "identity": {"version": 598},
        "output": {"count": 1},
    }
    ok = _grade((spec,), {"count": 1}, {"count": 0}, context=ctx)
    assert ok.results[0].outcome == "drift_allowed"
    assert ok.ok is True
    assert "witness_entry_at=200.0" in (ok.results[0].detail or "")
    # equal values need no witness
    eq = _grade((spec,), {"count": 1}, {"count": 1}, context=ctx)
    assert eq.results[0].outcome == "match"
    # empty history -> mismatch
    empty = _grade((spec,), {"count": 1}, {"count": 0},
                   context={**ctx, "witness_history": []})
    assert empty.results[0].outcome == "mismatch"
    assert empty.issues == ("tok_w",)
    # predicates unsatisfied -> mismatch
    bad = _grade((spec,), {"count": 1}, {"count": 0},
                 context={**ctx, "witness_history": [
                     {"at": 50.0, "previous_count": 0, "previous_last_version": 597}]})
    assert bad.results[0].outcome == "mismatch"
    # missing witness context -> mismatch (fail-closed)
    miss = _grade((spec,), {"count": 1}, {"count": 0}, context={})
    assert miss.results[0].outcome == "mismatch"


def test_k8_directional_subset_rules():
    spec = pr.FieldSpec("members", "directional_subset", "container", "tok_dir", {})
    # live lost members (reap happened) -> drift_allowed
    shrunk = _grade((spec,), {"members": {"a": 1, "b": 2, "c": 3}},
                    {"members": {"a": 1, "b": 2}})
    assert shrunk.results[0].outcome == "drift_allowed"
    assert shrunk.ok is True
    # identical -> match
    assert _grade((spec,), {"members": {"a": 1}}, {"members": {"a": 1}}).results[0].outcome == "match"
    # new live member -> mismatch
    grew = _grade((spec,), {"members": {"a": 1}}, {"members": {"a": 1, "d": 4}})
    assert grew.results[0].outcome == "mismatch"
    assert "tok_dir" in grew.issues
    # common member diverged -> mismatch
    div = _grade((spec,), {"members": {"a": 1}}, {"members": {"a": 9}})
    assert div.results[0].outcome == "mismatch"
    # live None (source cleaned by janitor) -> skip as drift_allowed
    gone = _grade((spec,), {"members": {"a": 1}}, {})
    assert gone.results[0].outcome == "drift_allowed"
    # list-shaped members: subset semantics by membership
    lists = _grade((spec,), {"members": ["a", "b", "c"]}, {"members": ["a", "c"]})
    assert lists.results[0].outcome == "drift_allowed"


def test_k9_monotonic_floor_rules():
    spec = pr.FieldSpec("end_offset", "monotonic_floor", "counter",
                        "archive rotation watermark behind: {name}", {})
    adv = _grade((spec,), {"end_offset": 100}, {"end_offset": 200},
                 context={"name": "events"})
    assert adv.results[0].outcome == "drift_allowed"
    assert adv.ok is True
    assert _grade((spec,), {"end_offset": 100}, {"end_offset": 100},
                  context={"name": "events"}).results[0].outcome == "match"
    behind = _grade((spec,), {"end_offset": 200}, {"end_offset": 100},
                    context={"name": "events"})
    assert behind.results[0].outcome == "mismatch"
    assert behind.issues == ("archive rotation watermark behind: events",)


def test_k10_shape_only_rules():
    spec = pr.FieldSpec("main_commit_oid", "shape_only", "frozen_observation",
                        "tok_shape", {"hex": 40, "provenance": "handoff.local_publication_proof/main_commit_oid"})
    # any 40-hex matches regardless of the live value (P17)
    ok = _grade((spec,), {"main_commit_oid": A}, {"main_commit_oid": B})
    assert ok.results[0].outcome == "match"
    assert ok.ok is True
    bad = _grade((spec,), {"main_commit_oid": "nothex"}, {"main_commit_oid": "nothex"})
    assert bad.results[0].outcome == "mismatch"
    assert "tok_shape" in bad.issues


def test_k11_reissue_digest_rules():
    spec = pr.FieldSpec("signal_sha256", "reissue_digest", "container", "tok_r", {})
    assert _grade((spec,), {"signal_sha256": D64}, {"signal_sha256": D64}).results[0].outcome == "match"
    bad = _grade((spec,), {"signal_sha256": D64}, {"signal_sha256": E64})
    assert bad.results[0].outcome == "mismatch"
    assert bad.issues == ("tok_r",)


# ---------------------------------------------------------------------------
# run_reproof plumbing
# ---------------------------------------------------------------------------


def test_run_reproof_unknown_site_raises():
    with pytest.raises(pr.ContractError):
        pr.run_reproof("no.such.site", frozen={}, live={})


def test_issues_property_collects_mismatch_tokens_in_order():
    result = _grade(
        (pr.FieldSpec("a", "exact", "container", "tok_a", {}),
         pr.FieldSpec("b", "exact", "container", "tok_b", {})),
        {"a": 1, "b": 1},
        {"a": 2, "b": 3},
    )
    assert result.ok is False
    assert result.issues == ("tok_a", "tok_b")


def test_default_probe_not_required_for_non_git_contract():
    # probe=None must not construct DefaultGitProbe (or touch git) when no
    # field needs it.
    result = _grade((pr.FieldSpec("x", "exact", "container", "tok_x", {}),),
                    {"x": 1}, {"x": 1}, probe=None)
    assert result.ok is True


def test_registered_sites_and_contract_for_roundtrip():
    sites = pr.registered_sites()
    assert "handoff.local_publication_proof" in sites
    assert "handoff.remote_publication" in sites
    assert "handoff.remote_identity" in sites
    assert "stability.daemon_identity" in sites
    assert "handoff.housekeeping.reap_proofs" in sites
    assert "archivist.reap_proof" in sites
    assert "archivist.reap_remote_proof" in sites
    contract = pr.contract_for("handoff.local_publication_proof")
    assert contract.site_id == "handoff.local_publication_proof"
    assert all(isinstance(f, pr.FieldSpec) for f in contract.fields)


def test_result_immutability_of_inputs():
    frozen = {"box": {"a": 1}}
    live = {"box": {"a": 1}}
    _grade((pr.FieldSpec("box", "stable_subset", "container", "tok", {"stable_keys": ["a"]}),),
           frozen, live)
    assert frozen == {"box": {"a": 1}}
    assert live == {"box": {"a": 1}}


# ---------------------------------------------------------------------------
# Final-review blockers: fail-open closures (K3 reverse alias, K8 None-skip,
# strict_log member deletion) and the wired/parked status marker
# ---------------------------------------------------------------------------


def test_k8_member_none_value_does_not_skip_comparison():
    # Final-review blocker 1: `if fv is None or lv is None: continue` made a
    # live member with a None VALUE bypass the comparison entirely and pass
    # as match. The old inline rule is plain whole-dict equality.
    spec = pr.FieldSpec("members", "directional_subset", "container", "tok_dir", {})
    bad = _grade((spec,), {"members": {"a": 1}}, {"members": {"a": None}})
    assert bad.results[0].outcome == "mismatch"
    assert bad.ok is False
    # plain-equality None == None on both sides stays a match (old != semantics)
    both = _grade((spec,), {"members": {"a": None}}, {"members": {"a": None}})
    assert both.results[0].outcome == "match"
    # list-shaped membership (pool reap targets) keeps working
    lists = _grade((spec,), {"members": ["a", "b", "c"]}, {"members": ["a", "c"]})
    assert lists.results[0].outcome == "drift_allowed"


def test_k1_exact_absent_live_skips_param():
    spec = pr.FieldSpec("tree", "exact", "container", "tok_t",
                        {"absent_live_skips": True})
    gone = _grade((spec,), {"tree": {"a": 1}}, {})
    assert gone.results[0].outcome == "drift_allowed"
    assert gone.ok is True
    assert gone.results[0].detail == "live_absent_skip"
    assert _grade((spec,), {"tree": {"a": 1}}, {"tree": {"a": 1}}).results[0].outcome == "match"
    # a surviving live side is graded by FULL equality: losing a member fails
    shrunk = _grade((spec,), {"tree": {"a": 1, "b": 2}}, {"tree": {"a": 1}})
    assert shrunk.results[0].outcome == "mismatch"
    nulled = _grade((spec,), {"tree": {"a": 1}}, {"tree": {"a": None}})
    assert nulled.results[0].outcome == "mismatch"


def test_k3_one_way_alias_direction():
    # Final-review blocker 2a: the #24 alias was ONE-WAY (official-expected
    # -> staging-observed). The engine must reproduce that direction, not a
    # symmetric any-spelling-to-any-spelling bridge.
    spec = pr.FieldSpec(
        "meta", "spelling_alias", "container", "tok_alias:{key}",
        {"alias_groups": {"canonical": ("official-key", "staging-key", "bare-key")},
         "expected_key_fallback": {"official-key": "staging-key"},
         "live_key_preimage": {"staging-key": "bare-key"},
         "value_shape": "single_item_list_str"},
    )
    # forward (#24 direction): official-expected, staging-observed -> match
    fwd = _grade((spec,), {"meta": {"official-key": "V"}},
                 {"meta": {"staging-key": ["V"]}})
    assert fwd.results[0].outcome == "match"
    # legacy bare key pre-mapped onto staging -> match
    bare = _grade((spec,), {"meta": {"staging-key": "V"}},
                  {"meta": {"bare-key": ["V"]}})
    assert bare.results[0].outcome == "match"
    # REVERSE direction must NOT pass (old producer:
    # completion_tag_metadata_mismatch:<expected key>)
    rev = _grade((spec,), {"meta": {"staging-key": "V"}},
                 {"meta": {"official-key": ["V"]}})
    assert rev.ok is False
    assert rev.issues == ("tok_alias:staging-key",)
    # empty expected mapping fails closed (no vacuous pass)
    empty = _grade((spec,), {"meta": {}}, {"meta": {"official-key": ["V"]}})
    assert empty.ok is False
    # expected key outside the declared alias set fails closed
    alien = _grade((spec,), {"meta": {"other-key": "V"}},
                   {"meta": {"other-key": ["V"]}})
    assert alien.ok is False


def test_k3_empty_expected_is_mismatch_param():
    # Final-review blocker 2b engine half: a present-but-empty expectation
    # plus a missing observation was a hard failure in the old scalar
    # fallback (`not expected_hash`); sites declare that posture explicitly.
    strict = pr.FieldSpec(
        "h", "spelling_alias", "container", "tok_h",
        {"alias_groups": {"c": ("c",)}, "none_normalization": True,
         "value_shape": "scalar", "empty_expected_is_mismatch": True},
    )
    bad = _grade((strict,), {"h": ""}, {"h": None})
    assert bad.results[0].outcome == "mismatch"
    assert _grade((strict,), {"h": "V"}, {"h": "V"}).results[0].outcome == "match"
    # the DEFAULT keeps the certificate-site semantics: None <-> '' bridges
    cert = pr.FieldSpec(
        "c", "spelling_alias", "container", "tok_c",
        {"alias_groups": {"c": ("c",)}, "none_normalization": True,
         "value_shape": "scalar"},
    )
    assert _grade((cert,), {"c": None}, {"c": ""}).results[0].outcome == "match"


def test_status_marker_validated_defaulted_and_queryable():
    # Final-review blocker 3: registrations must distinguish wired from
    # parked. Unknown status values are a declaration error; the DEFAULT is
    # parked (a registration alone proves no wiring).
    with pytest.raises(pr.ContractError):
        pr.register_contract(pr.ReproofContract(
            site_id=f"test.bad.{next(_SEQ)}",
            fields=(pr.FieldSpec("x", "exact", "container", "tok", {}),),
            status="maybe",
        ))
    sid = _tmp_site((pr.FieldSpec("x", "exact", "container", "tok", {}),))
    assert pr.contract_for(sid).status == "parked"
    assert sid in pr.parked_sites()
    assert sid not in pr.wired_sites()
