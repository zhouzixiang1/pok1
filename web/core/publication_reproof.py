"""Pure frozen-vs-live reproof judgment engine (Stage A, migration 2026-10-22).

This module is the single deterministic judgment surface for the
publication-reproof family (the 24+ exact-pin convergence defects): it takes
(contract, frozen, live, probe) inputs and returns typed verdicts.  It never
reads or writes files, never touches the network, and never performs git
write operations; its only external dependency is the injected GitProbe.
Effects (REISSUE_DIGEST rewrites, DIRECTIONAL_SUBSET disk enumeration,
archive evidence reads, the conditional tracking-ref fetch before remote
ancestry checks) stay with the calling adapters, per the AGENTS.md
deterministic-discipline rule ("Do not add a second ... authority"): the
engine holds no publication authority of any kind.

Review blockers digested here (design revision 2):

* B1 -- container key policy is explicit: a site whose mapping IS a record
  dict (``handoff.local_publication_proof``) declares every key as a field
  or a LIVE_OBSERVATION exclusion, so ``proof_digest``/``main_commit_oid``
  are declared-and-excluded instead of silently ignored, and any other key
  fails closed with ``undeclared_field:<k>`` on EITHER side.
* B2 -- ``stability.daemon_identity`` is registered as HARD EXACT
  (stability_observation.py:882-892 today has no witness acceptance path);
  the witness semantics live only at ``handoff.stability_observation_step``
  (step_contracts.py:484-527, the Stage C U8 site).
* B3 -- ``handoff.remote_identity`` (and ``archivist.reap_remote_proof``)
  carry a declaration-bound
  ``requires_tracking_ref_fetch_before_ancestry`` adapter precondition
  mirroring the conditional fetch at post_publication_handoff.py:1514-1521
  and tool_commit_archivist_orchestrator.py:221-230; the engine enforces
  the declaration, the adapter owns the fetch.
* B4 -- ``degrade_when_live_unknown`` is bound to a (site, field) whitelist
  covering BOTH stability remote-tip subchecks (tip-vs-recorded-head and
  per-row publication-commit-on-remote-main), not a kind x class rule.
* B5 -- the #26 ancestor-rule body ``_identity_mismatches``
  (stability_observation.py:653-692, commit a7596d53) is registered as
  ``stability.identity_repository_head`` with its three ``_projection``
  call-site semantics (continuity + compare_process=False preflights).
* B6 -- the closed key policy covers top-level AND container-internal AND
  per-item keys, on both the frozen and the live side.

Issue tokens are byte-preserved from the current inline producers
(nonGoals#6).  Two deviations from the design's illustrative examples are
deliberate and documented at their registration sites: the design's
``handoff_live_main_commit_not_ancestral`` token does not exist anywhere in
current code (the ancestry failure folds into
``handoff_live_local_publication_proof_mismatch``), and new
``undeclared_field:<k>`` tokens appear only at judgment loci that currently
have NO token (silently-ignored extra keys).

Final-review fixes (2026-10-23, second revision):

* strict-log source trees grade by FULL equality again (``exact`` with
  ``absent_live_skips``): the interim directional_subset registration
  allowed live manifests to LOSE members and let None-valued members bypass
  comparison -- both fail-open against the historical
  ``live_tree != expected_tree`` rule.  The K8 kind itself no longer skips
  None-valued member comparisons.
* the completion-tag alias is ONE-WAY again (``expected_key_fallback`` /
  ``live_key_preimage``): a staging-* expectation never accepts an
  official-* observation, matching the #24 producer; scalar sites may
  declare ``empty_expected_is_mismatch`` for the old ``not expected_hash``
  hard failure (the wired bot_artifact adapter currently gates that shape
  adapter-side).
* every catalog registration declares its wiring status (``wired`` /
  ``parked``, default parked) so rehearsal contracts can never masquerade
  as live judgments; ``handoff.publication_identity`` additionally gained
  the cert-less None/'' normalization it needs before any Stage-B③ wiring.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, NamedTuple, Protocol, Sequence

__all__ = [
    "ContractError",
    "GitProbe",
    "DefaultGitProbe",
    "FieldSpec",
    "ReproofContract",
    "FieldResult",
    "ReproofResult",
    "register_contract",
    "run_reproof",
    "registered_sites",
    "contract_for",
    "wired_sites",
    "parked_sites",
]


class ContractError(Exception):
    """Declaration-time error (raised at register_contract import time)."""


# ---------------------------------------------------------------------------
# Frozen engine API (v1) -- B/C/D migration tracks code against this surface
# ---------------------------------------------------------------------------


class GitProbe(Protocol):
    def is_ancestor(self, frozen_oid: str, live_oid: str) -> bool:
        """merge-base --is-ancestor; the ONLY failure semantics is False
        (fail-closed: git rc!=0, missing objects, timeouts, OSError all
        fold to False)."""
        ...

    def object_locally_known(self, oid: str) -> bool:
        """Valid ONLY for ls-remote branch tips (cat-file -e oid^{commit});
        must NEVER be used for tag objects."""
        ...


class DefaultGitProbe:
    """Lazy delegation to the EXISTING shared git seams -- no new git call
    path is introduced:

    * ``is_ancestor`` -> ``evolution_infra._git_command_succeeds``
      ("merge-base", "--is-ancestor", ...) -- the same seam behind
      ``post_publication_handoff._publication_commit_is_ancestor_of`` and
      ``tool_commit_archivist._publication_head_advance_allowed``;
    * ``object_locally_known`` -> ``stability_observation
      ._remote_tip_locally_known`` (cat-file -e oid^{commit}).
    """

    def is_ancestor(self, frozen_oid: str, live_oid: str) -> bool:
        from evolution_infra import _git_command_succeeds

        return bool(
            _git_command_succeeds(
                "merge-base", "--is-ancestor", str(frozen_oid), str(live_oid)
            )
        )

    def object_locally_known(self, oid: str) -> bool:
        from stability_observation import _remote_tip_locally_known

        return bool(_remote_tip_locally_known(str(oid)))


class FieldSpec(NamedTuple):
    name: str                  # key in the frozen/live mapping
    kind: str                  # one of FIELD_KINDS; unknown -> ContractError
    field_class: str           # one of FIELD_CLASSES; kind x class
                               # incompatibility -> ContractError
    issue_token: str           # token emitted on mismatch ({name}/{field}/
                               # {key}/{index} placeholders supported)
    params: Mapping[str, Any]  # per-kind params, validated at declaration


class ReproofContract(NamedTuple):
    site_id: str                       # e.g. "handoff.local_publication_proof"
    fields: tuple[FieldSpec, ...]
    key_policy: str = "closed"         # closed: a top-level key (either side)
                                       # that is neither a declared field nor
                                       # excluded -> mismatch
                                       # ("undeclared_field:<k>")
    exclude_keys: tuple[str, ...] = () # LIVE_OBSERVATION keys excluded at
                                       # construction; each must be in
                                       # KNOWN_LIVE_OBSERVATION_KEYS
    status: str = "parked"             # "wired": a production adapter calls
                                       # run_reproof on this site today (or a
                                       # wired parent reaches it through
                                       # item_contract/nested_contract);
                                       # "parked": rehearsal contract -- the
                                       # real comparison is still hand-written
                                       # at the cited locus, or a module-local
                                       # twin serves the wired locus.  The
                                       # DEFAULT is parked: a registration
                                       # alone proves no wiring.  Flipping a
                                       # site to wired belongs to the same
                                       # change that wires its adapter.


class FieldResult(NamedTuple):
    field: str
    outcome: Literal["match", "drift_allowed", "mismatch"]
    issue_token: str | None
    detail: str | None          # e.g. "ancestor_accepted" /
                                # "witness_entry_at=1792665119.826667" /
                                # "undeclared_field:<k>"
    stable_view: Any | None     # normalized projection (frozen-anchored) for
                                # digest re-derivation; invariant under
                                # allowed drift


class ReproofResult(NamedTuple):
    site_id: str
    ok: bool
    results: tuple[FieldResult, ...]

    @property
    def issues(self) -> tuple[str, ...]:
        return tuple(
            row.issue_token
            for row in self.results
            if row.outcome == "mismatch" and row.issue_token
        )


def register_contract(contract: ReproofContract) -> None:
    """Import-time registration.  Duplicate site_id, unknown kind, kind x
    field_class violation, forbidden params (degrade outside the stability
    whitelist, alias value transforms, gated witness sources, SHAPE_ONLY
    without provenance) all raise ContractError -- the "compile-time"
    rejection face.  A contract that passes validation but duplicates a
    site_id also fails here."""
    _validate_contract(contract)
    if contract.site_id in _REGISTRY:
        raise ContractError(f"duplicate_site:{contract.site_id}")
    _REGISTRY[contract.site_id] = contract


def run_reproof(
    site_id: str,
    *,
    frozen: Mapping[str, Any],
    live: Mapping[str, Any],
    probe: GitProbe | None = None,      # default DefaultGitProbe; tests
                                        # inject stubs
    context: Mapping[str, Any] | None = None,  # ungated witness history and
                                        # per-site context (token {name}/
                                        # {index}, predicate RHS paths)
) -> ReproofResult:
    contract = _REGISTRY.get(site_id)
    if contract is None:
        raise ContractError(f"unknown_site:{site_id}")
    return _run(
        contract,
        frozen=frozen,
        live=live,
        probe=probe if probe is not None else DefaultGitProbe(),
        context=dict(context or {}),
    )


def registered_sites() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def wired_sites() -> tuple[str, ...]:
    """Sites whose contract declares a production adapter (status="wired")."""
    return tuple(
        site for site, contract in _REGISTRY.items()
        if contract.status == "wired"
    )


def parked_sites() -> tuple[str, ...]:
    """Rehearsal-only registrations (status="parked"): trust no wiring."""
    return tuple(
        site for site, contract in _REGISTRY.items()
        if contract.status == "parked"
    )


def contract_for(site_id: str) -> ReproofContract:
    contract = _REGISTRY.get(site_id)
    if contract is None:
        raise ContractError(f"unknown_site:{site_id}")
    return contract


# ---------------------------------------------------------------------------
# Kind registry (K1-K11) and declaration-time validation
# ---------------------------------------------------------------------------

FIELD_KINDS = (
    "exact",             # K1: precise value/byte equality (type-sensitive)
    "exact_digest",      # K2: 64-hex digest equality with shape pre-check
    "spelling_alias",    # K3: key-name alias sets; values stay exact
    "git_ancestry",      # K4: equal-or-ancestor via the GitProbe seam
    "stable_subset",     # K5: container over declared stable keys
    "keyset_exclude",    # K6: keyset difference minus declared live keys
    "witness_reset",     # K7: process-scoped reset history witness
    "directional_subset",  # K8: live members may only disappear
    "monotonic_floor",   # K9: live counter may only advance
    "shape_only",        # K10: only the frozen value's shape is graded
    "reissue_digest",    # K11: adapter-rederived digest equality
)

FIELD_CLASSES = (
    "container",           # nested record structure
    "frozen_observation",  # git-observed value at freeze time; may advance
    "live_observation",    # exclusion justification class only (no kind
                           # pairs with it)
    "process_scoped",      # process-lifetime identity (witness applies)
    "publication_window",  # hard-exact window site (drift == real conflict)
    "counter",             # monotonically advancing scalar
)

_KIND_CLASS_ALLOWED: dict[str, frozenset[str]] = {
    # K1/K2 exactness is forbidden on frozen_observation git values (the
    # #1-16/#19/#21 defect class: an exact pin on a moving observation).
    "exact": frozenset({"container", "publication_window"}),
    "exact_digest": frozenset({"container", "publication_window"}),
    "spelling_alias": frozenset({"container"}),
    "git_ancestry": frozenset({"frozen_observation"}),
    "stable_subset": frozenset({"container"}),
    "keyset_exclude": frozenset({"container"}),
    "witness_reset": frozenset({"process_scoped"}),
    "directional_subset": frozenset({"container"}),
    "monotonic_floor": frozenset({"counter"}),
    "shape_only": frozenset({"frozen_observation"}),
    "reissue_digest": frozenset({"container", "publication_window"}),
}

_GENERIC_PARAMS = frozenset({"shared_token_group"})

_KIND_PARAMS: dict[str, frozenset[str]] = {
    "exact": frozenset({"none_pairs_match", "hex", "absent_live_skips"}),
    "exact_digest": frozenset(),
    "spelling_alias": frozenset({
        "alias_groups", "none_normalization", "value_shape",
        "expected_key_fallback", "live_key_preimage",
        "empty_expected_is_mismatch",
    }),
    "git_ancestry": frozenset({
        "tip_kind", "hex", "equal_passes", "degrade_when_live_unknown",
        "degrade_requires", "requires_tracking_ref_fetch_before_ancestry",
    }),
    "stable_subset": frozenset({
        "stable_keys", "container_exclude_keys", "none_pairs_match",
        "per_item_key", "item_contract", "nested_contract",
        "nested_contracts", "digest_over",
    }),
    "keyset_exclude": frozenset({"exclude_keys"}),
    "witness_reset": frozenset({"witness_source", "predicates", "entry_level_fields"}),
    "directional_subset": frozenset({"none_live_skips"}),
    "monotonic_floor": frozenset(),
    "shape_only": frozenset({"hex", "provenance"}),
    "reissue_digest": frozenset(),
}

# B4: the #22 degrade posture is a SITE property (residual exposure declared
# at stability_observation.py:840-844 for exactly these two subchecks), not
# a generic kind rule.
_DEGRADE_SITE_ALLOWLIST = frozenset({
    ("stability.remote_publication", "remote_main_vs_recorded_head"),
    ("stability.remote_publication_row", "publication_commit_on_remote_main"),
})

# B3: sites whose CURRENT code conditionally fetches the tracking ref before
# the ancestry check (post_publication_handoff.py:1514-1521 and
# tool_commit_archivist_orchestrator.py:221-230).  Registering the
# precondition elsewhere is refused; the adapters must keep the fetch.
_FETCH_PRECONDITION_SITES = frozenset({
    "handoff.remote_identity",
    "archivist.reap_remote_proof",
})

# N2: contract-level exclusions must be known LIVE_OBSERVATION keys.
KNOWN_LIVE_OBSERVATION_KEYS = frozenset({
    # post_publication_handoff.py:554-574 / 678-687
    "main_commit_oid", "proof_digest",
    "remote_main_oid", "valid", "issues",
    # tool_commit_archivist_orchestrator.py:238-244
    "publication_remote_main_oid", "verified_remote_main_oid",
})

# B1: container-level exclusions carry an explicit reason token.
_LIVE_EXCLUSION_REASONS = frozenset({
    "rederived_after_head_advance",
    "live_wire_observation",
    "verification_time_observation",
})

_WITNESS_PREDICATES = frozenset({
    "at_gte", "previous_last_version_eq", "previous_count_gte",
})

_REGISTRY: dict[str, ReproofContract] = {}

_MISSING = object()


def _is_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


def _absent(value: Any) -> bool:
    return value is _MISSING or value is None


def _require_str_list(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ContractError(f"{what}_not_nonempty_sequence")
    for item in value:
        if not isinstance(item, str) or not item:
            raise ContractError(f"{what}_entry_not_name")
    return tuple(value)


def _validate_contract(contract: ReproofContract) -> None:
    if not isinstance(contract.site_id, str) or not contract.site_id:
        raise ContractError("site_id_empty")
    if not isinstance(contract.fields, tuple) or not contract.fields:
        raise ContractError("fields_empty")
    if contract.key_policy != "closed":
        raise ContractError(f"key_policy_unsupported:{contract.key_policy}")
    if contract.status not in ("wired", "parked"):
        raise ContractError(f"status_invalid:{contract.status}")
    for key in contract.exclude_keys:
        if key not in KNOWN_LIVE_OBSERVATION_KEYS:
            raise ContractError(f"exclude_key_not_live_observation:{key}")
    seen_names: set[str] = set()
    groups: dict[str, list[str]] = {}
    for spec in contract.fields:
        if not isinstance(spec, FieldSpec):
            raise ContractError("field_not_fieldspec")
        if not isinstance(spec.name, str) or not spec.name:
            raise ContractError("field_name_empty")
        if spec.name in seen_names:
            raise ContractError(f"duplicate_field:{spec.name}")
        seen_names.add(spec.name)
        if spec.kind not in FIELD_KINDS:
            raise ContractError(f"unknown_kind:{spec.kind}")
        if spec.field_class not in FIELD_CLASSES:
            raise ContractError(f"unknown_field_class:{spec.field_class}")
        if spec.field_class not in _KIND_CLASS_ALLOWED[spec.kind]:
            raise ContractError(
                f"kind_class_mismatch:{spec.kind}:{spec.field_class}:{spec.name}"
            )
        if not isinstance(spec.issue_token, str) or not spec.issue_token:
            raise ContractError(f"issue_token_empty:{spec.name}")
        params = spec.params
        if not isinstance(params, Mapping):
            raise ContractError(f"params_not_mapping:{spec.name}")
        allowed = _KIND_PARAMS[spec.kind] | _GENERIC_PARAMS
        for key in params:
            if key not in allowed:
                # C4 guard: ANY extra parameter (in particular any value
                # transformer on spelling_alias) is refused.
                raise ContractError(f"param_not_allowed:{spec.kind}:{spec.name}:{key}")
        _validate_kind_params(contract, spec)
        group = params.get("shared_token_group")
        if group is not None:
            groups.setdefault(str(group), []).append(spec.issue_token)
    for group, tokens in groups.items():
        if len(set(tokens)) != 1:
            # R5: multi-field single-token sites must fill the SAME string.
            raise ContractError(f"shared_token_group_inconsistent:{group}")


def _validate_kind_params(contract: ReproofContract, spec: FieldSpec) -> None:
    kind = spec.kind
    params = spec.params
    if kind == "exact":
        if "hex" in params and (
            type(params["hex"]) is not int or params["hex"] <= 0
        ):
            raise ContractError(f"exact_hex_invalid:{spec.name}")
        if "none_pairs_match" in params and not isinstance(
            params["none_pairs_match"], bool
        ):
            raise ContractError(f"none_pairs_match_not_bool:{spec.name}")
        if "absent_live_skips" in params and not isinstance(
            params["absent_live_skips"], bool
        ):
            raise ContractError(f"absent_live_skips_not_bool:{spec.name}")
    elif kind == "git_ancestry":
        if params.get("tip_kind") not in ("head", "literal_oid"):
            raise ContractError(f"tip_kind_invalid:{spec.name}")
        if params.get("hex") is not None and params.get("hex") != 40:
            raise ContractError(f"ancestry_hex_not_40:{spec.name}")
        if params.get("equal_passes", True) is not True:
            raise ContractError(f"equal_passes_must_be_true:{spec.name}")
        if params.get("degrade_when_live_unknown", False):
            if (contract.site_id, spec.name) not in _DEGRADE_SITE_ALLOWLIST:
                # C3/B4: degrade is site-whitelisted, never generic.
                raise ContractError(
                    f"degrade_not_whitelisted:{contract.site_id}:{spec.name}"
                )
            if params.get("tip_kind") != "literal_oid":
                raise ContractError(f"degrade_requires_literal_tip:{spec.name}")
            if not params.get("degrade_requires"):
                raise ContractError(f"degrade_requires_missing:{spec.name}")
        if params.get("requires_tracking_ref_fetch_before_ancestry", False) and (
            contract.site_id not in _FETCH_PRECONDITION_SITES
        ):
            raise ContractError(
                f"fetch_precondition_not_bound:{contract.site_id}:{spec.name}"
            )
    elif kind == "spelling_alias":
        groups = params.get("alias_groups")
        if not isinstance(groups, Mapping) or len(groups) != 1:
            raise ContractError(f"alias_groups_shape:{spec.name}")
        for _canonical, names in groups.items():
            # C4: alias groups cover KEY NAMES only; any non-string entry
            # (callable/dict value transformer) is refused here.
            _require_str_list(names, f"alias_group:{spec.name}")
        name_set = set(next(iter(groups.values())))
        for param_key in ("expected_key_fallback", "live_key_preimage"):
            mapping = params.get(param_key, {})
            if not isinstance(mapping, Mapping):
                raise ContractError(f"{param_key}_shape:{spec.name}")
            for src, dst in mapping.items():
                if src not in name_set or dst not in name_set:
                    raise ContractError(
                        f"{param_key}_outside_alias_set:{spec.name}:{src}->{dst}"
                    )
        if "empty_expected_is_mismatch" in params and not isinstance(
            params["empty_expected_is_mismatch"], bool
        ):
            raise ContractError(
                f"empty_expected_is_mismatch_not_bool:{spec.name}"
            )
        if params.get("value_shape", "scalar") not in (
            "scalar", "single_item_list_str"
        ):
            raise ContractError(f"value_shape_invalid:{spec.name}")
        if "none_normalization" in params and not isinstance(
            params["none_normalization"], bool
        ):
            raise ContractError(f"none_normalization_not_bool:{spec.name}")
    elif kind == "witness_reset":
        source = params.get("witness_source")
        if not isinstance(source, str) or not source.startswith("ungated_state."):
            # C5: a gated projection source can never prove a reset.
            raise ContractError(f"witness_source_gated:{spec.name}")
        predicates = params.get("predicates")
        if not isinstance(predicates, Mapping) or not predicates:
            raise ContractError(f"predicates_shape:{spec.name}")
        for name in predicates:
            if name not in _WITNESS_PREDICATES:
                raise ContractError(f"predicate_unknown:{spec.name}:{name}")
    elif kind == "shape_only":
        if type(params.get("hex")) is not int or params.get("hex", 0) <= 0:
            raise ContractError(f"shape_only_hex_invalid:{spec.name}")
        if not isinstance(params.get("provenance"), str) or not params.get(
            "provenance"
        ):
            # C6: shape-only judgments must name their authoritative source.
            raise ContractError(f"shape_only_provenance_missing:{spec.name}")
    elif kind == "stable_subset":
        nested = params.get("nested_contract")
        per_item = params.get("per_item_key")
        nested_map = params.get("nested_contracts")
        mode_count = sum(
            1 for present in (nested, per_item, nested_map) if present
        )
        if mode_count > 1:
            raise ContractError(f"stable_subset_modes_exclusive:{spec.name}")
        if nested is not None:
            if not isinstance(nested, str) or not nested:
                raise ContractError(f"nested_contract_shape:{spec.name}")
            if nested in _REGISTRY:
                _validate_contract_reference(nested)
            for extra in ("stable_keys", "container_exclude_keys"):
                if extra in params:
                    raise ContractError(
                        f"nested_contract_exclusive:{spec.name}:{extra}"
                    )
        elif nested_map is not None:
            if not isinstance(nested_map, Mapping) or not nested_map:
                raise ContractError(f"nested_contracts_shape:{spec.name}")
            for key, site in nested_map.items():
                if not isinstance(key, str) or not isinstance(site, str):
                    raise ContractError(f"nested_contracts_shape:{spec.name}")
                if site in _REGISTRY:
                    _validate_contract_reference(site)
        elif per_item is not None:
            if not isinstance(per_item, str) or not per_item:
                raise ContractError(f"per_item_key_shape:{spec.name}")
            item_contract = params.get("item_contract")
            if not isinstance(item_contract, str) or not item_contract:
                raise ContractError(f"item_contract_shape:{spec.name}")
            if item_contract in _REGISTRY:
                _validate_contract_reference(item_contract)
        else:
            keys = params.get("stable_keys")
            _require_str_list(keys, f"stable_keys:{spec.name}")
            exclusions = params.get("container_exclude_keys", {})
            if not isinstance(exclusions, Mapping):
                raise ContractError(
                    f"container_exclude_keys_shape:{spec.name}"
                )
            for key, reason in exclusions.items():
                if key in set(keys):
                    raise ContractError(
                        f"container_exclude_overlaps_stable:{spec.name}:{key}"
                    )
                if reason not in _LIVE_EXCLUSION_REASONS:
                    raise ContractError(
                        f"container_exclude_reason_unknown:{spec.name}:{key}"
                    )
        if params.get("digest_over") not in (None, "stable_view"):
            raise ContractError(f"digest_over_invalid:{spec.name}")
    elif kind == "keyset_exclude":
        keys = params.get("exclude_keys", ())
        _require_str_list(keys, f"keyset_exclude:{spec.name}")
        for key in keys:
            if key not in KNOWN_LIVE_OBSERVATION_KEYS:
                raise ContractError(
                    f"keyset_exclude_not_live_observation:{spec.name}:{key}"
                )


def _validate_contract_reference(site_id: str) -> None:
    # Reference cycles between registered contracts are a declaration error.
    seen = {site_id}
    stack = [site_id]
    while stack:
        current = _REGISTRY[stack.pop()]
        for spec in current.fields:
            for ref_key in ("nested_contract", "item_contract"):
                ref = spec.params.get(ref_key)
                if ref in _REGISTRY:
                    if ref in seen:
                        raise ContractError(f"contract_reference_cycle:{ref}")
                    seen.add(ref)
                    stack.append(ref)
            for ref in (spec.params.get("nested_contracts") or {}).values():
                if ref in _REGISTRY:
                    if ref in seen:
                        raise ContractError(f"contract_reference_cycle:{ref}")
                    seen.add(ref)
                    stack.append(ref)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

Outcome = Literal["match", "drift_allowed", "mismatch"]


def _render(token: str, names: Mapping[str, Any]) -> str:
    rendered = token
    for key, value in names.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def _names_for(spec: FieldSpec, context: Mapping[str, Any]) -> dict[str, Any]:
    names: dict[str, Any] = {"field": spec.name}
    for key in ("name", "index"):
        if key in context:
            names[key] = context[key]
    return names


def _field_names(spec: FieldSpec, context: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    names = _names_for(spec, context)
    names.update(extra)
    return names


def _run(
    contract: ReproofContract,
    *,
    frozen: Mapping[str, Any],
    live: Mapping[str, Any],
    probe: Any,
    context: Mapping[str, Any],
    extra_allowed: frozenset[str] = frozenset(),
) -> ReproofResult:
    results: list[FieldResult] = []
    for spec in contract.fields:
        frozen_value = (
            frozen.get(spec.name, _MISSING)
            if isinstance(frozen, Mapping) else _MISSING
        )
        live_value = (
            live.get(spec.name, _MISSING)
            if isinstance(live, Mapping) else _MISSING
        )
        results.append(
            _grade_field(spec, frozen_value, live_value, probe, context)
        )
    if contract.key_policy == "closed":
        declared = {spec.name for spec in contract.fields}
        declared |= set(contract.exclude_keys) | set(extra_allowed)
        union: set[str] = set()
        if isinstance(frozen, Mapping):
            union |= set(frozen)
        if isinstance(live, Mapping):
            union |= set(live)
        for key in sorted(union - declared):
            results.append(
                FieldResult(
                    field=key,
                    outcome="mismatch",
                    issue_token=f"undeclared_field:{key}",
                    detail="undeclared_key",
                    stable_view=None,
                )
            )
    ok = all(row.outcome != "mismatch" for row in results)
    return ReproofResult(contract.site_id, ok, tuple(results))


def _grade_field(
    spec: FieldSpec,
    frozen_value: Any,
    live_value: Any,
    probe: Any,
    context: Mapping[str, Any],
) -> FieldResult:
    names = _names_for(spec, context)
    grade = _GRADES[spec.kind]
    return grade(spec, frozen_value, live_value, probe, context, names)


def _match(spec: FieldSpec, detail: str, stable_view: Any, names) -> FieldResult:
    return FieldResult(spec.name, "match", None, detail, stable_view)


def _drift(spec: FieldSpec, detail: str, stable_view: Any, names) -> FieldResult:
    return FieldResult(spec.name, "drift_allowed", None, detail, stable_view)


def _mismatch(spec: FieldSpec, detail: str, names, stable_view: Any = None) -> FieldResult:
    return FieldResult(
        spec.name,
        "mismatch",
        _render(spec.issue_token, names),
        detail,
        stable_view,
    )


# --- K1 exact ---------------------------------------------------------------


def _grade_exact(spec, f, l, probe, ctx, names):
    params = spec.params
    # Final-review blocker 1: sites whose historical rule was
    # "live side absent -> skip; present -> FULL equality" (the strict-log
    # source tree) declare that shape explicitly; the skip is drift_allowed
    # and every surviving byte of the live side is then compared.
    if params.get("absent_live_skips", False) and _absent(l):
        return _drift(spec, "live_absent_skip", f, names)
    none_pairs_match = bool(params.get("none_pairs_match", False))
    hex_len = params.get("hex")
    if hex_len is not None and not (
        _is_hex(f, hex_len) and _is_hex(l, hex_len)
    ):
        return _mismatch(spec, "shape_invalid", names)
    if _absent(f) and _absent(l):
        if none_pairs_match:
            return _match(spec, "none_pair_allowed", None, names)
        # red-team semantics (post_publication_handoff.py:621-633):
        # None == None on a stable field is a shape break, never a match.
        return _mismatch(spec, "none_pair_shape_break", names)
    if _absent(f) or _absent(l):
        return _mismatch(spec, "value_absent_one_side", names)
    if f == l:
        return _match(spec, "value_equal", f, names)
    return _mismatch(spec, "value_mismatch", names)


# --- K2 exact_digest --------------------------------------------------------


def _grade_exact_digest(spec, f, l, probe, ctx, names):
    if not _is_hex(f, 64) or not _is_hex(l, 64):
        return _mismatch(spec, "shape_invalid", names)
    if f == l:
        return _match(spec, "digest_equal", f, names)
    return _mismatch(spec, "digest_mismatch", names)


# --- K3 spelling_alias ------------------------------------------------------


def _resolve_alias(side: Any, key_names: Sequence[str]):
    if side is _MISSING:
        return None, _MISSING
    if not isinstance(side, Mapping):
        # scalar field value (e.g. a top-level digest): the value itself is
        # the resolved payload, carried under the canonical alias key name.
        return key_names[0], side
    for key in key_names:
        if key in side:
            return key, side[key]
    return None, _MISSING


def _norm_empty(value: Any, enabled: bool) -> Any:
    if enabled and (value is None or value == ""):
        return None
    return value


def _alias_live_rows(
    live: Any,
    expected_key: str,
    fallback: Mapping[str, str],
    preimage: Mapping[str, str],
):
    """Resolve the live rows for one EXPECTED key under the ONE-WAY rule.

    Final-review blocker 2a: the historical alias (bot_artifact.py:822-836,
    the #24 fix) is directional -- an official-* EXPECTATION may fall back to
    the staging-* observation, and the live bare legacy key pre-maps onto
    staging.  The reverse direction (a staging expectation accepting an
    official observation) was NEVER legal and must keep failing.  Resolution
    chain per expected key: [expected_key, expected_key_fallback[expected_
    key]], each candidate also tried through its live_key_preimage.
    """
    chain = [expected_key]
    if expected_key in fallback:
        chain.append(fallback[expected_key])
    if not isinstance(live, Mapping):
        # scalar observation (e.g. the resolved tag artifact hash): it
        # answers every expected key directly (per-key shape comparison
        # still applies).
        return live
    for candidate in chain:
        if candidate in live:
            return live[candidate]
        pre = preimage.get(candidate)
        if pre is not None and pre in live:
            return live[pre]
    return _MISSING


def _grade_spelling_alias(spec, f, l, probe, ctx, names):
    params = spec.params
    groups = params["alias_groups"]
    canonical = next(iter(groups))
    key_names = tuple(groups[canonical])
    name_set = set(key_names)
    none_norm = bool(params.get("none_normalization", True))
    shape = params.get("value_shape", "scalar")
    fallback = dict(params.get("expected_key_fallback", {}) or {})
    preimage = dict(params.get("live_key_preimage", {}) or {})
    empty_expected_is_mismatch = bool(
        params.get("empty_expected_is_mismatch", False)
    )

    if not isinstance(f, Mapping):
        # scalar field value: single-key normalization bridge only.
        if f is _MISSING:
            return _mismatch(spec, "alias_key_absent", names)
        fkey = key_names[0]
        fval = _norm_empty(f, none_norm)
        if empty_expected_is_mismatch and fval is None:
            return _mismatch(spec, "expected_empty", names)
        if not isinstance(l, Mapping):
            lkey = fkey
            lval = _norm_empty(l, none_norm)
        else:
            lkey, lraw = _resolve_alias(l, key_names)
            if lkey is None:
                return _mismatch(spec, "alias_key_absent", names)
            lval = _norm_empty(lraw, none_norm)
        token_names = dict(names)
        token_names["key"] = fkey
        if shape == "scalar":
            if fval == lval:
                return _match(spec, f"alias:{fkey}~{lkey}", {fkey: fval}, token_names)
            return _mismatch(spec, f"alias_value_mismatch:{lkey}", token_names)
        if fval is None and lval is None:
            return _match(spec, f"alias:{fkey}~{lkey}", {fkey: None}, token_names)
        if isinstance(fval, str) and isinstance(lval, list) and lval == [fval]:
            return _match(spec, f"alias:{fkey}~{lkey}", {fkey: fval}, token_names)
        return _mismatch(spec, f"alias_value_mismatch:{lkey}", token_names)

    # mapping-valued frozen side: the expected metadata itself.  EVERY
    # expected key grades (the old producer loops expected_metadata.items());
    # an empty expectation is a shape break, never a vacuous pass.
    if not f:
        return _mismatch(spec, "expected_metadata_empty", names)
    for key in f:
        if key not in name_set:
            return _mismatch(spec, f"expected_key_not_declared:{key}", names)
    for key in sorted(f):
        token_names = dict(names)
        token_names["key"] = key
        expected = _norm_empty(f[key], none_norm)
        if empty_expected_is_mismatch and expected is None:
            return FieldResult(
                spec.name, "mismatch",
                _render(spec.issue_token, token_names),
                "expected_empty", None,
            )
        raw = _alias_live_rows(l, key, fallback, preimage)
        observed = _norm_empty(raw if raw is not _MISSING else None, none_norm)
        if shape == "single_item_list_str":
            if expected is None and observed is None:
                continue
            if (
                isinstance(expected, str)
                and isinstance(observed, list)
                and observed == [expected]
            ):
                continue
            return _mismatch(spec, f"alias_value_mismatch:{key}", token_names)
        if expected == observed:
            continue
        return _mismatch(spec, f"alias_value_mismatch:{key}", token_names)
    stable_view = {key: f[key] for key in sorted(f)}
    return _match(spec, "alias_one_way_match", stable_view, names)


# --- K4 git_ancestry --------------------------------------------------------


def _grade_git_ancestry(spec, f, l, probe, ctx, names):
    params = spec.params
    if not _is_hex(f, 40):
        return _mismatch(spec, "frozen_not_hex_40", names)
    if not _is_hex(l, 40):
        return _mismatch(spec, "live_not_hex_40", names)
    if f == l:
        return _match(spec, "equal", f, names)
    if params.get("degrade_when_live_unknown", False):
        if not probe.object_locally_known(l):
            return _drift(spec, "live_unknown_degraded", f, names)
    if probe.is_ancestor(f, l):
        return _drift(spec, "ancestor_accepted", f, names)
    # Probe failure folds to False: fail-closed (#19 semantics).
    return _mismatch(spec, "not_ancestor_or_probe_failed", names, stable_view=f)


# --- K5 stable_subset -------------------------------------------------------


def _aggregate(children: list[tuple[str, FieldResult]]) -> tuple[str, str | None, str | None, Any]:
    """Aggregate (key, child) pairs into (outcome, issue_token, detail,
    stable_view)."""
    for key, child in children:
        if child.outcome == "mismatch":
            detail = f"item:{key}:{child.detail}"
            return "mismatch", child.issue_token, detail, None
    stable_view = []
    any_drift = False
    for key, child in children:
        if child.outcome == "drift_allowed":
            any_drift = True
        stable_view.append(child.stable_view)
    if any_drift:
        return "drift_allowed", None, "items_drift_allowed", stable_view
    return "match", None, "items_match", stable_view


def _grade_stable_subset(spec, f, l, probe, ctx, names):
    params = spec.params
    nested = params.get("nested_contract")
    if nested is not None:
        sub_contract = _REGISTRY.get(nested)
        if sub_contract is None:
            raise ContractError(f"unknown_site:{nested}")
        sub = _run(
            sub_contract, frozen=f or {}, live=l or {}, probe=probe,
            context=ctx, extra_allowed=frozenset(),
        )
        bad = next(
            (r for r in sub.results if r.outcome == "mismatch"), None
        )
        if bad is not None:
            return FieldResult(
                spec.name, "mismatch", bad.issue_token,
                f"nested:{bad.field}:{bad.detail}", None,
            )
        stable_view = {
            r.field: r.stable_view
            for r in sub.results
            if r.outcome != "mismatch"
        }
        outcome = (
            "drift_allowed"
            if any(r.outcome == "drift_allowed" for r in sub.results)
            else "match"
        )
        return FieldResult(spec.name, outcome, None, outcome, stable_view)
    per_item_key = params.get("per_item_key")
    if per_item_key is not None:
        return _grade_per_item(spec, f, l, probe, ctx, names, per_item_key)
    return _grade_flat_container(spec, f, l, probe, ctx, names)


def _grade_per_item(spec, f, l, probe, ctx, names, per_item_key):
    item_site = params_site = spec.params.get("item_contract")
    sub_contract = _REGISTRY.get(item_site)
    if sub_contract is None:
        raise ContractError(f"unknown_site:{item_site}")
    if not isinstance(f, list) or not isinstance(l, list):
        return _mismatch(spec, "items_not_list", names)
    for side, label in ((f, "frozen"), (l, "live")):
        for item in side:
            if not isinstance(item, Mapping):
                return _mismatch(spec, f"item_not_object:{label}", names)

    def keyed(side):
        index: dict[Any, Mapping] = {}
        for item in side:
            raw_key = item.get(per_item_key)
            if isinstance(raw_key, bool) or not isinstance(raw_key, (str, int)):
                return None, raw_key
            key = raw_key if isinstance(raw_key, str) else int(raw_key)
            if not str(key):
                return None, raw_key
            if key in index:
                return "duplicate", key
            index[key] = item
        return index, None

    f_index, f_err = keyed(f)
    l_index, l_err = keyed(l)
    if f_err is not None:
        return _mismatch(spec, f"frozen_item_key_invalid:{f_err}", names)
    if l_err is not None:
        return _mismatch(spec, f"live_item_key_invalid:{l_err}", names)
    for key in sorted(l_index):
        if key not in f_index:
            return _mismatch(spec, f"undeclared_item:{key}", names)
    for key in sorted(f_index):
        if key not in l_index:
            return _mismatch(spec, f"missing_item:{key}", names)
    children: list[tuple[str, FieldResult]] = []
    for key in sorted(f_index):
        item_ctx = dict(ctx)
        item_ctx["index"] = key
        item_names = dict(names)
        item_names["index"] = key
        sub = _run(
            sub_contract,
            frozen=f_index[key],
            live=l_index[key],
            probe=probe,
            context=item_ctx,
            extra_allowed=frozenset({per_item_key}),
        )
        bad = next((r for r in sub.results if r.outcome == "mismatch"), None)
        if bad is not None:
            return FieldResult(
                spec.name,
                "mismatch",
                bad.issue_token,
                f"item:{key}:{bad.field}:{bad.detail}",
                None,
            )
        if any(r.outcome == "drift_allowed" for r in sub.results):
            children.append((key, FieldResult(
                key, "drift_allowed", None, "item_drift",
                {r.field: r.stable_view for r in sub.results
                 if r.outcome != "mismatch"},
            )))
        else:
            children.append((key, FieldResult(
                key, "match", None, "item_match",
                {r.field: r.stable_view for r in sub.results
                 if r.outcome != "mismatch"},
            )))
    outcome, token, detail, stable_view = _aggregate(children)
    return FieldResult(spec.name, outcome, token, detail, stable_view)


def _grade_flat_container(spec, f, l, probe, ctx, names):
    params = spec.params
    if not isinstance(f, Mapping) or not isinstance(l, Mapping):
        return _mismatch(spec, "container_not_mapping", names)
    stable_keys = list(params.get("stable_keys", ()))
    exclusions = dict(params.get("container_exclude_keys", {}))
    nested_map = params.get("nested_contracts") or {}
    none_pairs_match = bool(params.get("none_pairs_match", False))
    allowed = set(stable_keys) | set(exclusions) | set(nested_map)
    for key in sorted(set(f) | set(l)):
        if key not in allowed:
            return _mismatch(spec, f"undeclared_field:{key}", names)
    for key in stable_keys:
        fv = f.get(key, _MISSING)
        lv = l.get(key, _MISSING)
        if _absent(fv) and _absent(lv):
            if not none_pairs_match:
                return _mismatch(spec, f"stable_key_absent:{key}", names)
            continue
        if _absent(fv) or _absent(lv):
            return _mismatch(spec, f"stable_key_absent:{key}", names)
        if fv != lv:
            return _mismatch(spec, f"stable_key_mismatch:{key}", names)
    stable_view: dict[str, Any] = {
        key: f.get(key) for key in stable_keys if key in f
    }
    any_drift = False
    for key, site_id in nested_map.items():
        sub_contract = _REGISTRY.get(site_id)
        if sub_contract is None:
            raise ContractError(f"unknown_site:{site_id}")
        sub = _run(
            sub_contract,
            frozen=f.get(key) or {},
            live=l.get(key) or {},
            probe=probe,
            context=ctx,
            extra_allowed=frozenset(),
        )
        bad = next((r for r in sub.results if r.outcome == "mismatch"), None)
        if bad is not None:
            return FieldResult(
                spec.name, "mismatch", bad.issue_token,
                f"nested:{key}:{bad.field}:{bad.detail}", None,
            )
        if any(r.outcome == "drift_allowed" for r in sub.results):
            any_drift = True
        stable_view[key] = {
            r.field: r.stable_view
            for r in sub.results
            if r.outcome != "mismatch"
        }
    if any_drift:
        return _drift(spec, "nested_drift_allowed", stable_view, names)
    return _match(spec, "stable_match", stable_view, names)


# --- K6 keyset_exclude ------------------------------------------------------


def _grade_keyset_exclude(spec, f, l, probe, ctx, names):
    if not isinstance(f, Mapping) or not isinstance(l, Mapping):
        return _mismatch(spec, "container_not_mapping", names)
    excl = set(spec.params.get("exclude_keys", ()))
    fkeys = set(f) - excl
    lkeys = set(l) - excl
    if fkeys != lkeys:
        drift_keys = ",".join(sorted(fkeys ^ lkeys))
        return _mismatch(spec, f"keyset_drift:{drift_keys}", names)
    for key in sorted(fkeys):
        fv = f.get(key, _MISSING)
        lv = l.get(key, _MISSING)
        if _absent(fv) or _absent(lv):
            return _mismatch(spec, f"key_none:{key}", names)
        if fv != lv:
            return _mismatch(spec, f"key_value_mismatch:{key}", names)
    stable_view = {key: f[key] for key in sorted(fkeys)}
    return _match(spec, "keyset_match", stable_view, names)


# --- K7 witness_reset -------------------------------------------------------


def _resolve_path(ctx: Mapping[str, Any], path: str) -> Any:
    node: Any = ctx
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _predicate_ok(entry: Mapping[str, Any], name: str, rhs: Any) -> bool:
    if name == "at_gte":
        at = entry.get("at")
        return (
            isinstance(at, (int, float))
            and not isinstance(at, bool)
            and isinstance(rhs, (int, float))
            and not isinstance(rhs, bool)
            and at >= rhs
        )
    if name == "previous_last_version_eq":
        return entry.get("previous_last_version") == rhs
    if name == "previous_count_gte":
        count = entry.get("previous_count")
        return (
            type(count) is int
            and isinstance(rhs, int)
            and not isinstance(rhs, bool)
            and count >= rhs
        )
    return False


def _grade_witness_reset(spec, f, l, probe, ctx, names):
    if f == l:
        return _match(spec, "value_equal", f, names)
    history = ctx.get("witness_history")
    if not isinstance(history, list):
        return _mismatch(spec, "witness_history_unavailable", names)
    predicates = spec.params["predicates"]
    rhs: dict[str, Any] = {}
    for name, path in predicates.items():
        value = _resolve_path(ctx, path)
        if value is _MISSING:
            return _mismatch(spec, f"witness_context_missing:{path}", names)
        rhs[name] = value
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        if all(_predicate_ok(entry, name, rhs[name]) for name in rhs):
            # entry-level fields (meta-lesson 1): the witness carries its own
            # {at, previous_count, previous_last_version} values
            stable_view = {
                key: entry.get(key)
                for key in ("at", "previous_count", "previous_last_version")
                if key in entry
            }
            return _drift(
                spec, f"witness_entry_at={entry.get('at')}", stable_view, names
            )
    return _mismatch(spec, "witness_entry_not_found", names)


# --- K8 directional_subset --------------------------------------------------


def _as_member_map(value: Any):
    if isinstance(value, Mapping):
        return {key: value[key] for key in value}
    if isinstance(value, list):
        return {item: None for item in value if isinstance(item, (str, int))}
    return None


def _grade_directional_subset(spec, f, l, probe, ctx, names):
    if _absent(l):
        if spec.params.get("none_live_skips", True):
            return _drift(spec, "live_none_skip", f, names)
        return _mismatch(spec, "live_absent", names)
    f_map = _as_member_map(f)
    l_map = _as_member_map(l)
    if f_map is None or l_map is None:
        return _mismatch(spec, "container_shape_invalid", names)
    extra = sorted(str(key) for key in l_map if key not in f_map)
    if extra:
        return _mismatch(spec, f"new_live_member:{extra[0]}", names)
    for key in l_map:
        fv = f_map.get(key)
        lv = l_map.get(key)
        # Final-review blocker 1: no None bypass -- a member whose live VALUE
        # is None is compared like any other (old whole-dict != semantics).
        # List-shaped sides normalize every member to None, so membership
        # grading is unaffected.
        if fv != lv:
            return _mismatch(spec, f"member_value_mismatch:{key}", names)
    if set(l_map) == set(f_map):
        return _match(spec, "members_equal", f, names)
    removed = len(set(f_map) - set(l_map))
    return _drift(spec, f"members_removed:{removed}", f, names)


# --- K9 monotonic_floor -----------------------------------------------------


def _grade_monotonic_floor(spec, f, l, probe, ctx, names):
    if type(f) is not int or type(l) is not int:
        return _mismatch(spec, "counter_not_int", names)
    if l == f:
        return _match(spec, "floor_equal", f, names)
    if l > f:
        return _drift(spec, "floor_advanced", f, names)
    return _mismatch(spec, "floor_behind", names, stable_view=f)


# --- K10 shape_only ---------------------------------------------------------


def _grade_shape_only(spec, f, l, probe, ctx, names):
    hex_len = spec.params["hex"]
    if _is_hex(f, hex_len):
        return _match(spec, f"shape_only:{hex_len}", f, names)
    return _mismatch(spec, "shape_invalid", names)


# --- K11 reissue_digest -----------------------------------------------------


def _grade_reissue_digest(spec, f, l, probe, ctx, names):
    if not _is_hex(f, 64) or not _is_hex(l, 64):
        return _mismatch(spec, "shape_invalid", names)
    if f == l:
        return _match(spec, "reissue_digest_equal", f, names)
    return _mismatch(spec, "reissue_digest_mismatch", names)


_GRADES = {
    "exact": _grade_exact,
    "exact_digest": _grade_exact_digest,
    "spelling_alias": _grade_spelling_alias,
    "git_ancestry": _grade_git_ancestry,
    "stable_subset": _grade_stable_subset,
    "keyset_exclude": _grade_keyset_exclude,
    "witness_reset": _grade_witness_reset,
    "directional_subset": _grade_directional_subset,
    "monotonic_floor": _grade_monotonic_floor,
    "shape_only": _grade_shape_only,
    "reissue_digest": _grade_reissue_digest,
}


# ---------------------------------------------------------------------------
# Production site catalog
# ---------------------------------------------------------------------------
# Token byte-preservation notes (nonGoals#6): every token below is the exact
# string the CURRENT inline producer emits, except `undeclared_field:<k>`
# (new judgment loci that previously had no token at all).
#
# Wiring status (final-review blocker 3): every registration declares
# status="wired" (a production adapter calls run_reproof on this site today,
# directly or through item_contract/nested_contract from a wired parent --
# evidence: `grep -rn 'run_reproof(\s*"' web/core` plus the variable-selected
# handoff.housekeeping.reap_proofs call in post_publication_handoff_step_
# contracts.py) or status="parked" (rehearsal contract: the producer still
# hand-writes the comparison at the cited locus, or a module-local twin
# serves the wired locus).  The registration default is parked -- a
# registration alone proves no wiring -- and flipping a site to wired
# belongs to the same change that wires its adapter.

_PROOF_TOKEN = "handoff_live_local_publication_proof_mismatch"

# Site 1 (B1): the mapping IS the local publication proof dict; the 11
# canonical keys (_LOCAL_PROOF_KEYS, post_publication_handoff.py:74-78) are
# 9 publication-bound exact fields + the main_commit_oid ancestry field +
# the proof_digest LIVE_OBSERVATION exclusion (rederived after any HEAD
# advance; never comparable by design).  Ancestry failure folds into the
# SAME token the current _same_publication_proof emits.
register_contract(ReproofContract(
    site_id="handoff.local_publication_proof",
    fields=(
        FieldSpec("schema_version", "exact", "container", _PROOF_TOKEN, {}),
        FieldSpec("kind", "exact", "container", _PROOF_TOKEN, {}),
        FieldSpec("bot", "exact", "container", _PROOF_TOKEN, {}),
        FieldSpec("version", "exact", "container", _PROOF_TOKEN, {}),
        FieldSpec("artifact_hash", "exact", "container", _PROOF_TOKEN, {}),
        FieldSpec("tag", "exact", "container", _PROOF_TOKEN, {}),
        FieldSpec("tag_object", "exact", "container", _PROOF_TOKEN, {}),
        FieldSpec("commit_oid", "exact", "container", _PROOF_TOKEN, {}),
        FieldSpec("completion_tree_oid", "exact", "container", _PROOF_TOKEN, {}),
        FieldSpec(
            "main_commit_oid", "git_ancestry", "frozen_observation",
            _PROOF_TOKEN,
            {"tip_kind": "head", "hex": 40, "equal_passes": True},
        ),
    ),
    exclude_keys=("proof_digest",),
))

_IDENTITY_TOKEN = "another_post_publication_handoff_is_active"

# Site 2 (R1; final-review blocker 3② -- PARKED): the remote publication
# dict, required-remote shape only.  The WIRED locus is the module-local
# ``handoff.remote_publication_keyset`` (post_publication_handoff.py: a
# keyset_exclude grade that also covers the local-only binding era's
# ``explicit_test_mode`` key and None tip).  This catalog site is the
# Stage-B② rehearsal for the required-remote shape: publication-bound keys
# exact, ``valid``/``issues`` the declared LIVE_OBSERVATION exclusions (the
# historical live-field union, _REMOTE_PUBLICATION_LIVE_FIELDS), remote tip
# under the ancestor rule.
register_contract(ReproofContract(
    site_id="handoff.remote_publication",
    fields=(
        FieldSpec("required", "exact", "container", _IDENTITY_TOKEN, {}),
        FieldSpec("paired_refs", "exact", "container", _IDENTITY_TOKEN, {}),
        FieldSpec(
            "remote_main_oid", "git_ancestry", "frozen_observation",
            _IDENTITY_TOKEN,
            {"tip_kind": "literal_oid", "hex": 40, "equal_passes": True},
        ),
    ),
    exclude_keys=("valid", "issues"),
    status="parked",
))

# Site 3: live re-proof legs -- certificate None/'' normalization
# (post_publication_handoff.py:1462-1472) and annotated-tag object/peeled
# exactness (:1473-1480).
register_contract(ReproofContract(
    site_id="handoff.local_certificate_and_refs",
    fields=(
        FieldSpec(
            "certificate_digest", "spelling_alias", "container",
            "handoff_live_certificate_digest_mismatch",
            {"alias_groups": {"certificate_digest": ("certificate_digest",)},
             "none_normalization": True, "value_shape": "scalar"},
        ),
        FieldSpec(
            "tag_object", "exact", "container",
            "handoff_live_tag_object_mismatch:{name}", {"hex": 40},
        ),
        FieldSpec(
            "tag_commit", "exact", "container",
            "handoff_live_tag_commit_mismatch:{name}", {"hex": 40},
        ),
    ),
))

# Site 4 (B3): remote identity leg.  The ancestry field carries the
# declaration-bound tracking-ref fetch precondition -- the adapter MUST run
# the conditional fetch from post_publication_handoff.py:1514-1521 before
# grading, or an un-fetched advanced tip collapses DefaultGitProbe to False
# and re-creates the #19/#22 wedge class.
register_contract(ReproofContract(
    site_id="handoff.remote_identity",
    fields=(
        FieldSpec(
            "commit_on_remote_main", "git_ancestry", "frozen_observation",
            "handoff_commit_not_on_remote_main",
            {"tip_kind": "literal_oid", "hex": 40, "equal_passes": True,
             "requires_tracking_ref_fetch_before_ancestry": True},
        ),
        FieldSpec(
            "remote_tag_object", "exact", "container",
            "handoff_live_remote_tag_object_mismatch:{name}", {"hex": 40},
        ),
        FieldSpec(
            "remote_tag_commit", "exact", "container",
            "handoff_live_remote_tag_commit_mismatch:{name}", {"hex": 40},
        ),
    ),
))

# Site 5: frozen-row remote tip rule (#19, stability_observation.py:459-487).
# Both values come from the frozen row (publication commit vs recorded tip).
register_contract(ReproofContract(
    site_id="stability.row_remote_main",
    fields=(
        FieldSpec(
            "remote_main_oid", "git_ancestry", "frozen_observation",
            "observation_{index}_remote_main_not_publication_commit",
            {"tip_kind": "literal_oid", "hex": 40, "equal_passes": True},
        ),
    ),
))

# Site 6: recorded repository head vs latest publication commit
# (stability_observation.py:537-555).
register_contract(ReproofContract(
    site_id="stability.repository_head",
    fields=(
        FieldSpec(
            "repository_head", "git_ancestry", "frozen_observation",
            "repository_head_not_latest_publication_commit",
            {"tip_kind": "literal_oid", "hex": 40, "equal_passes": True},
        ),
    ),
))

# Site 7 (B5): the #26 ancestor-rule body (_identity_mismatches,
# stability_observation.py:653-692, commit a7596d53).  The frozen side is
# the state's recorded repository_head, the live side the current HEAD;
# exact-or-ancestor.  The current code reports the mismatching identity KEY
# name inside identity_mismatches, so the token IS the key name.
register_contract(ReproofContract(
    site_id="stability.identity_repository_head",
    fields=(
        FieldSpec(
            "repository_head", "git_ancestry", "frozen_observation",
            "repository_head",
            {"tip_kind": "head", "hex": 40, "equal_passes": True},
        ),
    ),
))

# Site 8: per-row sub-contract of the #22 degrade site (B4: BOTH ancestry
# subchecks are whitelisted for the degrade).
register_contract(ReproofContract(
    site_id="stability.remote_publication_row",
    fields=(
        FieldSpec(
            "publication_commit_on_remote_main", "git_ancestry",
            "frozen_observation",
            "v{index}:publication_commit_not_on_remote_main",
            {"tip_kind": "literal_oid", "hex": 40, "equal_passes": True,
             "degrade_when_live_unknown": True,
             "degrade_requires": "site_declares_ls_remote_reachability_first"},
        ),
        FieldSpec(
            "remote_tag_object", "exact", "container",
            "v{index}:remote_tag_object_mismatch:{name}", {},
        ),
        FieldSpec(
            "remote_tag_peeled", "exact", "container",
            "v{index}:remote_tag_peeled_mismatch:{name}", {},
        ),
    ),
))

# Site 9 (B4/P6): the stability live remote re-proof (#22).  A locally
# unknown live tip degrades BOTH ancestry subchecks; the tag string
# comparisons stay unconditional.
register_contract(ReproofContract(
    site_id="stability.remote_publication",
    fields=(
        FieldSpec(
            "remote_main_vs_recorded_head", "git_ancestry",
            "frozen_observation", "remote_main_head_mismatch",
            {"tip_kind": "literal_oid", "hex": 40, "equal_passes": True,
             "degrade_when_live_unknown": True,
             "degrade_requires": "site_declares_ls_remote_reachability_first"},
        ),
        FieldSpec(
            "rows", "stable_subset", "container",
            "v{index}:publication_commit_not_on_remote_main",
            {"per_item_key": "version",
             "item_contract": "stability.remote_publication_row"},
        ),
    ),
))

# Site 10 (B2 correction): daemon identity is HARD EXACT.  Current
# _live_daemon_errors (stability_observation.py:882-892) accepts no witness
# for a diverged live daemon; registering witness_reset here would have
# been a third, forbidden behavior relaxation (nonGoals#8).  The witness
# semantics belong to the step-contract site below.
register_contract(ReproofContract(
    site_id="stability.daemon_identity",
    fields=(
        FieldSpec(
            "daemon_process_identity", "exact", "container",
            "rating_daemon_process_identity_mismatch", {},
        ),
    ),
))

# Site 11 (U8/#12): the stability step witness.  The witness history is fed
# by the adapter from the UNGATED state read
# (stability_observation.read_ungated_reset_history, Stage C): the gated
# projection renders reset_history empty when continuity is invalid
# (stability_observation.py:988-993), which would make a legitimate reset
# witness invisible exactly when it is needed.  Predicates are byte-equal
# to step_contracts.py:519-526.
register_contract(ReproofContract(
    site_id="handoff.stability_observation_step",
    fields=(
        FieldSpec(
            "row", "witness_reset", "process_scoped",
            "handoff_stability_observation_reproof_mismatch",
            {"witness_source": "ungated_state.reset_history",
             "predicates": {
                 "at_gte": "receipt.completed_at",
                 "previous_last_version_eq": "identity.version",
                 "previous_count_gte": "output.count",
             },
             "entry_level_fields": True},
        ),
    ),
))

# Site 12 (P9/R5): housekeeping worktree ring -- one shared token across
# the three fields, exactly like step_contracts.py:753-770 today.
register_contract(ReproofContract(
    site_id="handoff.housekeeping",
    fields=(
        FieldSpec(
            "head_oid", "git_ancestry", "frozen_observation",
            "handoff_housekeeping_worktree_reproof_mismatch",
            {"tip_kind": "head", "hex": 40, "equal_passes": True,
             "shared_token_group": "worktree"},
        ),
        FieldSpec(
            "worktree_status_digest", "exact_digest", "container",
            "handoff_housekeeping_worktree_reproof_mismatch",
            {"shared_token_group": "worktree"},
        ),
        FieldSpec(
            "tracked_housekeeping_commit", "exact", "container",
            "handoff_housekeeping_worktree_reproof_mismatch",
            {"shared_token_group": "worktree"},
        ),
    ),
))

# Site 13 (U1/P10, innermost): the reap remote proof.  publication_remote_
# main_oid is the publication-time observation (excluded LIVE key);
# verified_remote_main_oid is graded under the ancestor rule WITH the
# tracking-ref fetch precondition (the orchestrator fetches before its
# ancestry check, tool_commit_archivist_orchestrator.py:221-230).
register_contract(ReproofContract(
    site_id="archivist.reap_remote_proof",
    fields=(
        FieldSpec("required", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec("tombstone_object_oid", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec("tombstone_commit_oid", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec("publication_commit_is_ancestor", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec(
            "verified_remote_main_oid", "git_ancestry", "frozen_observation",
            "handoff_housekeeping_reap_reproof_mismatch",
            {"tip_kind": "literal_oid", "hex": 40, "equal_passes": True,
             "requires_tracking_ref_fetch_before_ancestry": True},
        ),
    ),
    exclude_keys=("publication_remote_main_oid",),
))

# Site 14 (U1/P10): one reap proof per bot.  Every publication-bound key is
# declared: only the nested remote proof's remote oid legs may drift, and
# only ancestrally.
register_contract(ReproofContract(
    site_id="archivist.reap_proof",
    fields=(
        FieldSpec("bot", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec("version", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec("completion_commit_oid", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec("tombstone_tag", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec("tombstone_object_oid", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec("tombstone_commit_oid", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec("completed_sentinel_absent", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec("registry_projection_present", "exact", "container",
                  "handoff_housekeeping_reap_reproof_mismatch", {}),
        FieldSpec(
            "remote_proof", "stable_subset", "container",
            "handoff_housekeeping_reap_reproof_mismatch",
            {"nested_contract": "archivist.reap_remote_proof"},
        ),
    ),
))

# Site 15 (U1/P10): the housekeeping reap_proofs container.  Replaces the
# current whole-dict inequality (step_contracts.py:771-778) with per-bot
# graded fields; the set digest is re-derived over the stable_view
# projection (R4: both sides projected through the engine).
register_contract(ReproofContract(
    site_id="handoff.housekeeping.reap_proofs",
    fields=(
        FieldSpec(
            "reap_proofs", "stable_subset", "container",
            "handoff_housekeeping_reap_reproof_mismatch",
            {"per_item_key": "bot",
             "item_contract": "archivist.reap_proof",
             "digest_over": "stable_view"},
        ),
    ),
))

# Site 16 (R2/R5): the pool_reap five-field ring.  The current loop
# (step_contracts.py:697-704) has no None guard -- double-None passes --
# so the ring keeps none_pairs_match=True (composition note: upstream
# producers always emit real values; this preserves the leaf comparison
# byte-for-byte).  One shared token, like today.
register_contract(ReproofContract(
    site_id="archivist.pool_reap_ring",
    fields=tuple(
        FieldSpec(
            name, "exact", "container",
            "handoff_pool_reap_external_reproof_mismatch",
            {"none_pairs_match": True, "shared_token_group": "pool_ring"},
        )
        for name in (
            "version", "completion_commit_oid", "tombstone_tag",
            "tombstone_object_oid", "tombstone_commit_oid",
        )
    ),
))

# Site 17 (P11): the archivist worktree seam
# (tool_commit_archivist.py:154-215).  HEAD advance is graded by the same
# ancestor helper that becomes DefaultGitProbe's underlay; dirty drift
# keeps its own hard-failure token.
register_contract(ReproofContract(
    site_id="archivist.worktree",
    fields=(
        FieldSpec(
            "head_oid", "git_ancestry", "frozen_observation",
            "post_publication_head_changed",
            {"tip_kind": "head", "hex": 40, "equal_passes": True},
        ),
        FieldSpec(
            "worktree_status_digest", "exact_digest", "container",
            "post_publication_worktree_changed", {},
        ),
    ),
))

# Site 18 (P12; final-review blocker 3③ -- PARKED): rotation watermark
# monotonic floor.  The live comparison is still the hand-written
# `watermark["end_offset"] < plan["end_offset"]` check in
# evolution_infra_archive_rotation.py; this contract is the Stage-C
# rehearsal target.
register_contract(ReproofContract(
    site_id="rotation.watermark",
    fields=(
        FieldSpec(
            "end_offset", "monotonic_floor", "counter",
            "archive rotation watermark behind: {name}", {},
        ),
    ),
    status="parked",
))

# Site 19 (P13/R3; final-review blocker 2a): completion-tag metadata alias
# ring (bot_artifact.py:781-836).  The #24 alias is ONE-WAY: an official-*
# EXPECTATION may fall back to the staging-* observation, and the live bare
# legacy key (pre-2026-08-15 writer bug, v173/v185) pre-maps onto staging.
# The reverse direction (staging expectation accepting an official
# observation) was never legal and fails closed.  The VALUE stays an exact
# single-element list comparison, so duplicate rows ([v, v]) fail.
register_contract(ReproofContract(
    site_id="bot_artifact.completion_tag_metadata",
    fields=(
        FieldSpec(
            "candidate_hash", "spelling_alias", "container",
            "completion_tag_metadata_mismatch:{key}",
            {"alias_groups": {
                "candidate-hash": (
                    "official-candidate-hash",
                    "staging-candidate-hash",
                    "candidate-hash",
                ),
             },
             "expected_key_fallback": {
                 "official-candidate-hash": "staging-candidate-hash",
             },
             "live_key_preimage": {
                 "staging-candidate-hash": "candidate-hash",
             },
             "value_shape": "single_item_list_str"},
        ),
    ),
))

# Site 20 (P14; final-review blocker 1): strict log archives.  The
# historical rule is WHOLE-dict equality -- `live_tree is not None and
# live_tree != expected_tree` -- so a reaped live tree (v186 janitor) skips,
# and a surviving tree must match the frozen preimage EXACTLY: any missing,
# extra, changed, or None-valued member fails closed.  The earlier
# directional_subset registration allowed member deletion -- a fail-open
# relaxation of that rule -- and is withdrawn.
#
# PARKED: the WIRED locus is the archivist's module-local
# ``archivist.strict_log_source_tree`` (tool_commit_archivist.py) -- a
# stable_subset full-keyset grade that keeps the reaped-skip adapter-side.
# This catalog site is the pre-migration-rule rehearsal only.
register_contract(ReproofContract(
    site_id="archivist.strict_log_archives",
    fields=(
        FieldSpec(
            "source_tree", "exact", "container",
            "strict_log_final_source_mismatch", {"absent_live_skips": True},
        ),
    ),
    status="parked",
))

# Site 21 (P15): pool membership after the reap plan
# (tool_commit_archivist.py:1229-1234).  Live members may only disappear;
# a new member is pool_changed_after_reap_plan.  (The second rule there,
# unplanned_pool_member_removed over removed-vs-targets, is a composite
# adapter judgment and stays with the adapter.)
register_contract(ReproofContract(
    site_id="archivist.pool_reap_targets",
    fields=(
        FieldSpec(
            "active_bots", "directional_subset", "container",
            "pool_changed_after_reap_plan", {},
        ),
    ),
))

# Site 22 (P16; final-review blocker 3④ -- PARKED): publishing
# repo_baseline -- a publication-window site: ANY drift is a real conflict
# and stays fail-closed.  The live comparison is still hand-written
# (pipeline_recovery.py / evolution_infra_checkpoint_cas.py); this contract
# is the Stage-D registration-only rehearsal.
register_contract(ReproofContract(
    site_id="handoff.repo_baseline",
    fields=(
        FieldSpec("head", "exact", "publication_window",
                  "repo_baseline_head_mismatch", {}),
        FieldSpec("contract_hash", "exact", "publication_window",
                  "repo_baseline_contract_mismatch", {}),
    ),
    status="parked",
))

# Site 23 (P17): recorded main_commit_oid shape check
# (post_publication_handoff.py:1210-1217) -- a live checkout observation;
# structurally it must only be a well-formed commit oid.
register_contract(ReproofContract(
    site_id="handoff.shape_only.main_commit_oid",
    fields=(
        FieldSpec(
            "main_commit_oid", "shape_only", "frozen_observation",
            "handoff_local_publication_proof_identity_mismatch",
            {"hex": 40,
             "provenance": "handoff.local_publication_proof/main_commit_oid"},
        ),
    ),
))

# Sites 24/25: reissue digest comparisons (step_contracts.py:591-629).
# The adapter keeps the reissue (locked sidecar write + re-read); the
# engine only grades the digest.
register_contract(ReproofContract(
    site_id="handoff.reap_signal",
    fields=(
        FieldSpec("signal_sha256", "reissue_digest", "container",
                  "handoff_reap_signal_reproof_mismatch", {}),
    ),
))
register_contract(ReproofContract(
    site_id="handoff.priority_eval",
    fields=(
        FieldSpec("payload_sha256", "reissue_digest", "container",
                  "handoff_priority_eval_reproof_mismatch", {}),
    ),
))

# Site 26 (final-review blocker 3① -- PARKED, semantics corrected): the
# retry-stable publication identity spine (_same_publication_identity,
# post_publication_handoff.py).  The spine comparison is still the
# hand-written loop there; this contract is the Stage-B③ rehearsal target.
# certificate_digest is NOT an exact stable key: the cert-less era freezes
# None while the live spec resolves '' (the handoff module's own
# None/empty normalization), so it grades through the spelling_alias
# normalization instead -- an exact None==None pin here would re-wedge
# every cert-less publication the day this contract gets wired.
register_contract(ReproofContract(
    site_id="handoff.publication_identity",
    fields=(
        FieldSpec("schema_version", "exact", "container", _IDENTITY_TOKEN, {}),
        FieldSpec("evaluation_epoch", "exact", "container", _IDENTITY_TOKEN, {}),
        FieldSpec("version", "exact", "container", _IDENTITY_TOKEN, {}),
        FieldSpec("source_v", "exact", "container", _IDENTITY_TOKEN, {}),
        FieldSpec("workflow_run_id", "exact", "container", _IDENTITY_TOKEN, {}),
        FieldSpec("publication_id", "exact", "container", _IDENTITY_TOKEN, {}),
        FieldSpec("commit_oid", "exact", "container", _IDENTITY_TOKEN, {}),
        FieldSpec(
            "candidate_artifact_hash", "exact", "container",
            _IDENTITY_TOKEN, {},
        ),
        FieldSpec(
            "source_binding_digest", "exact", "container",
            _IDENTITY_TOKEN, {},
        ),
        FieldSpec("local_paired_refs", "exact", "container", _IDENTITY_TOKEN, {}),
        FieldSpec(
            "archive_base_snapshot_digest", "exact", "container",
            _IDENTITY_TOKEN, {},
        ),
        FieldSpec(
            "certificate_digest", "spelling_alias", "container",
            _IDENTITY_TOKEN,
            {"alias_groups": {"certificate_digest": ("certificate_digest",)},
             "none_normalization": True, "value_shape": "scalar"},
        ),
        FieldSpec(
            "local_publication_proof", "stable_subset", "container",
            _IDENTITY_TOKEN,
            {"nested_contract": "handoff.local_publication_proof"},
        ),
        FieldSpec(
            "remote_publication", "stable_subset", "container",
            _IDENTITY_TOKEN,
            {"nested_contract": "handoff.remote_publication"},
        ),
    ),
    status="parked",
))


# ---------------------------------------------------------------------------
# Wiring status evidence pass (final-review blocker 3)
# ---------------------------------------------------------------------------
# The 22 sites below have production adapters TODAY (verified 2026-10-23 by
# `grep -rn 'run_reproof(\s*"' web/core` against every non-engine module,
# plus the variable-selected call in post_publication_handoff_step_
# contracts.py that reaches handoff.housekeeping.reap_proofs and, through
# item_contract/nested_contract, archivist.reap_proof and
# archivist.reap_remote_proof).  Everything else stays at the parked
# default: handoff.publication_identity, handoff.remote_publication,
# rotation.watermark, handoff.repo_baseline, and
# archivist.strict_log_archives (whose wired twin is the archivist's
# module-local archivist.strict_log_source_tree).
_WIRED_SITES = (
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
)
for _wired_site in _WIRED_SITES:
    if _wired_site not in _REGISTRY:
        raise ContractError(f"wired_site_not_registered:{_wired_site}")
    _REGISTRY[_wired_site] = _REGISTRY[_wired_site]._replace(status="wired")
